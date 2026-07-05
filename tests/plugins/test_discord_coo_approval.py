"""Unit tests for Discord COO approval handler entry point (Phase 6C-1)."""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

from agent.coo.approval_report import build_approval_report
from agent.coo.approval_session import CEOApprovalSessionStore
from agent.coo.orchestrator import COOOrchestrator
import plugins.platforms.discord.coo_approval as coo_approval
from plugins.platforms.discord.coo_approval import (
    _calculate_embed_size,
    _COO_APPROVAL_VIEW_TIMEOUT_SECONDS,
    _EMBED_COLOR,
    _ERR_NOT_ALLOWED,
    _ERR_SESSION_NOT_FOUND,
    _is_terminal_approval_status,
    _make_coo_approval_button_callback,
    _should_disable_coo_approval_buttons,
    build_coo_approval_components,
    build_coo_approval_embed_payload,
    build_coo_approval_session_payload,
    build_discord_embed_from_payload,
    build_discord_view_from_components,
    coo_approval_error_message,
    execute_coo_approval_button_action,
    normalize_discord_snowflake,
    parse_coo_approval_custom_id,
    prepare_coo_approval_render_items,
)


def _make_fake_discord_module():
    class FakeButtonStyle:
        primary = "primary"
        success = "success"
        danger = "danger"
        secondary = "secondary"

    class FakeEmbed:
        def __init__(self, title="", description="", color=0):
            self.title = title
            self.description = description
            self.color = color
            self.fields = []
            self.footer_text = None

        def add_field(self, name="", value="", inline=False):
            self.fields.append({"name": name, "value": value, "inline": inline})

        def set_footer(self, text=""):
            self.footer_text = text

    class FakeButton:
        def __init__(self, label="", style=None, custom_id="", disabled=False):
            self.label = label
            self.style = style
            self.custom_id = custom_id
            self.disabled = disabled
            self.callback = None

    class FakeView:
        def __init__(self, timeout=None):
            self.timeout = timeout
            self.children = []

        def add_item(self, item):
            self.children.append(item)

    fake = types.SimpleNamespace()
    fake.Embed = FakeEmbed
    fake.ButtonStyle = FakeButtonStyle
    fake.ui = types.SimpleNamespace(View=FakeView, Button=FakeButton)
    return fake


_EXEC_APPROVAL_FORBIDDEN_COLORS = (
    0xE67E22,  # orange
    0x2ECC71,  # green
    0x3498DB,  # blue
    0x9B59B6,  # purple
)


def _sample_session_payload(**overrides: object) -> dict:
    payload = {
        "session_id": "11111111-2222-3333-4444-555555555555",
        "status": "pending",
        "task_kind": "daily_brief",
        "run_date": "2026-07-04",
        "report_status": "ready",
        "runtime_status": "selected",
        "requester_id": "987654321012345678",
        "channel_id": "111222333444555666",
        "execution_ticket_id": "",
        "execution_dispatched": False,
        "publish_dispatched": False,
    }
    payload.update(overrides)
    return payload


def _seed_session_in_store(
    store: CEOApprovalSessionStore,
    **overrides: object,
) -> dict:
    from agent.coo.approval_report import CEOApprovalReport, CEOApprovalReportStatus
    from agent.coo.approval_session import CEOApprovalSessionStatus, create_approval_session

    report = CEOApprovalReport(
        status=CEOApprovalReportStatus.READY,
        task_kind="daily_brief",
        run_date="2026-07-04",
        runtime_status="selected",
        worker_summary="test",
    )
    orchestrated = COOOrchestrator().orchestrate("???", run_date="2026-07-04")
    requester_id = str(overrides.pop("requester_id", "987654321012345678"))
    channel_id = str(overrides.pop("channel_id", "111222333444555666"))
    session = create_approval_session(
        report,
        orchestrated,
        requester_id=requester_id,
        channel_id=channel_id,
        store=store,
    )
    for key, value in overrides.items():
        if key == "status" and isinstance(value, str):
            setattr(session, key, CEOApprovalSessionStatus(value))
        else:
            setattr(session, key, value)
    store.save(session)
    return session.to_dict()


class TestDiscordCooApprovalEntryPoint(unittest.TestCase):
    def _ready_report_and_orchestration(self):
        import subprocess

        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            orchestrated = COOOrchestrator().orchestrate(
                "오늘 상태 보고해",
                run_date="2026-07-04",
            )
        return build_approval_report(orchestrated), orchestrated

    def test_normalize_discord_snowflake_accepts_int_and_str(self) -> None:
        self.assertEqual(normalize_discord_snowflake(987654321012345678), "987654321012345678")
        self.assertEqual(normalize_discord_snowflake("111222333444555666"), "111222333444555666")

    def test_build_payload_stores_discord_user_and_channel_ids(self) -> None:
        store = CEOApprovalSessionStore()
        report, orchestrated = self._ready_report_and_orchestration()
        payload = build_coo_approval_session_payload(
            report,
            orchestrated,
            discord_user_id=987654321012345678,
            discord_channel_id="111222333444555666",
            store=store,
        )

        self.assertIsInstance(payload, dict)
        assert payload is not None
        self.assertEqual(payload["requester_id"], "987654321012345678")
        self.assertEqual(payload["channel_id"], "111222333444555666")
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["execution_ticket_id"], "")
        self.assertFalse(payload["execution_dispatched"])
        self.assertFalse(payload["publish_dispatched"])

    def test_build_payload_returns_none_for_not_started_report(self) -> None:
        store = CEOApprovalSessionStore()
        orchestrated = COOOrchestrator().orchestrate("???", run_date="2026-07-04")
        report = build_approval_report(orchestrated)
        payload = build_coo_approval_session_payload(
            report,
            orchestrated,
            discord_user_id="987654321012345678",
            discord_channel_id="111222333444555666",
            store=store,
        )

        self.assertIsNone(payload)
        self.assertEqual(len(store.list_sessions()), 0)

    def test_entry_point_exposes_prepare_only_not_approve_reject(self) -> None:
        import plugins.platforms.discord.coo_approval as coo_entry

        self.assertTrue(hasattr(coo_entry, "build_coo_approval_session_payload"))
        self.assertTrue(hasattr(coo_entry, "normalize_discord_snowflake"))
        self.assertFalse(hasattr(coo_entry, "approve_discord_session"))
        self.assertFalse(hasattr(coo_entry, "reject_discord_session"))


class TestDiscordCooApprovalUiPayload(unittest.TestCase):
    def test_embed_payload_includes_core_session_fields(self) -> None:
        session = _sample_session_payload()
        embed = build_coo_approval_embed_payload(session)

        field_map = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertEqual(embed["title"], "Hermes COO Approval Required")
        self.assertIn(session["session_id"], field_map["Session ID"])
        self.assertEqual(field_map["Status"], "pending")
        self.assertEqual(field_map["Requester"], session["requester_id"])
        self.assertEqual(field_map["Channel"], session["channel_id"])
        self.assertEqual(field_map["Report Status"], "ready")
        self.assertEqual(field_map["Runtime Status"], "selected")
        self.assertIn("Approval only", embed["footer"]["text"])

    def test_embed_payload_color_distinct_from_exec_approval_colors(self) -> None:
        embed = build_coo_approval_embed_payload(_sample_session_payload())

        self.assertEqual(embed["color"], _EMBED_COLOR)
        for forbidden_color in _EXEC_APPROVAL_FORBIDDEN_COLORS:
            self.assertNotEqual(embed["color"], forbidden_color)

    def test_embed_payload_shows_execution_not_created_or_dispatched(self) -> None:
        embed = build_coo_approval_embed_payload(_sample_session_payload())
        field_map = {field["name"]: field["value"] for field in embed["fields"]}

        self.assertEqual(field_map["Execution Ticket"], "Not created")
        self.assertEqual(field_map["Execution"], "Not dispatched")
        self.assertEqual(field_map["Publish"], "Not dispatched")

    def test_components_include_approve_reject_refresh_with_prefix(self) -> None:
        session = _sample_session_payload()
        components = build_coo_approval_components(session)

        self.assertEqual(len(components), 3)
        labels = [button["label"] for button in components]
        self.assertEqual(labels, ["Approve", "Reject", "Refresh"])
        for button in components:
            self.assertTrue(button["custom_id"].startswith("coo_approval:"))
            self.assertIn(session["session_id"], button["custom_id"])

        self.assertEqual(
            components[0]["custom_id"],
            f"coo_approval:approve:{session['session_id']}",
        )
        self.assertEqual(
            components[1]["custom_id"],
            f"coo_approval:reject:{session['session_id']}",
        )
        self.assertEqual(
            components[2]["custom_id"],
            f"coo_approval:refresh:{session['session_id']}",
        )

    def test_components_builder_requires_session_id(self) -> None:
        with self.assertRaises(ValueError):
            build_coo_approval_components({})

    def test_embed_payload_truncates_long_field_values(self) -> None:
        long_session_id = "x" * 2000
        embed = build_coo_approval_embed_payload(
            _sample_session_payload(session_id=long_session_id)
        )
        field_map = {field["name"]: field["value"] for field in embed["fields"]}

        self.assertLessEqual(len(field_map["Session ID"]), 1024)
        self.assertTrue(field_map["Session ID"].endswith("..."))

    def test_execution_ticket_id_truncates_when_long(self) -> None:
        long_ticket = "ticket-" + ("x" * 3000)
        embed = build_coo_approval_embed_payload(
            _sample_session_payload(execution_ticket_id=long_ticket)
        )
        field_map = {field["name"]: field["value"] for field in embed["fields"]}

        self.assertLessEqual(len(field_map["Execution Ticket"]), 1024)
        self.assertTrue(field_map["Execution Ticket"].endswith("..."))

    def test_embed_total_length_stays_within_discord_limit(self) -> None:
        session = _sample_session_payload(
            session_id="s" * 2000,
            execution_ticket_id="t" * 3000,
            report_status="r" * 2000,
            runtime_status="x" * 2000,
            task_kind="k" * 800,
            requester_id="u" * 500,
            channel_id="c" * 500,
        )
        embed = build_coo_approval_embed_payload(session)

        self.assertLessEqual(_calculate_embed_size(embed), 6000)


class TestDiscordCooApprovalUiObjects(unittest.TestCase):
    def test_build_discord_embed_falls_back_without_discord_py(self) -> None:
        embed_payload = build_coo_approval_embed_payload(_sample_session_payload())
        with patch.object(coo_approval, "_get_discord_module", return_value=None):
            result = build_discord_embed_from_payload(embed_payload)

        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("_fallback"), "embed")
        self.assertEqual(result.get("title"), embed_payload["title"])

    def test_build_discord_view_falls_back_without_discord_py(self) -> None:
        components = build_coo_approval_components(_sample_session_payload())
        with patch.object(coo_approval, "_get_discord_module", return_value=None):
            result = build_discord_view_from_components(components)

        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("_fallback"), "view")
        self.assertEqual(len(result.get("components", [])), 3)
        self.assertEqual(result["components"][0]["custom_id"], components[0]["custom_id"])

    def test_build_discord_view_preserves_custom_ids_with_fake_discord(self) -> None:
        components = build_coo_approval_components(_sample_session_payload())
        fake_discord = _make_fake_discord_module()
        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord):
            view = build_discord_view_from_components(components)

        self.assertEqual(len(view.children), 3)
        custom_ids = [button.custom_id for button in view.children]
        self.assertEqual(custom_ids, [component["custom_id"] for component in components])
        for button in view.children:
            self.assertTrue(callable(button.callback))

    def test_coo_approval_view_timeout_matches_session_ttl(self) -> None:
        self.assertEqual(_COO_APPROVAL_VIEW_TIMEOUT_SECONDS, 86400)

    def test_build_discord_view_uses_session_ttl_timeout(self) -> None:
        components = build_coo_approval_components(_sample_session_payload())
        fake_discord = _make_fake_discord_module()
        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord):
            view = build_discord_view_from_components(components)

        self.assertEqual(view.timeout, _COO_APPROVAL_VIEW_TIMEOUT_SECONDS)

    def test_build_discord_embed_creates_fake_embed_object(self) -> None:
        embed_payload = build_coo_approval_embed_payload(_sample_session_payload())
        fake_discord = _make_fake_discord_module()
        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord):
            embed = build_discord_embed_from_payload(embed_payload)

        self.assertIsInstance(embed, fake_discord.Embed)
        self.assertEqual(embed.title, embed_payload["title"])
        self.assertEqual(len(embed.fields), len(embed_payload["fields"]))

    def test_ui_builders_do_not_call_approval_session_handlers(self) -> None:
        session = _sample_session_payload()
        embed_payload = build_coo_approval_embed_payload(session)
        components = build_coo_approval_components(session)
        with patch.object(coo_approval, "_get_discord_module", return_value=None), patch(
            "agent.coo.discord_approval_adapter.approve_discord_session"
        ) as mock_approve, patch(
            "agent.coo.discord_approval_adapter.reject_discord_session"
        ) as mock_reject, patch(
            "agent.coo.discord_approval_adapter.get_discord_approval_session"
        ) as mock_get:
            build_discord_embed_from_payload(embed_payload)
            build_discord_view_from_components(components)

        mock_approve.assert_not_called()
        mock_reject.assert_not_called()
        mock_get.assert_not_called()


class TestDiscordCooApprovalRenderWiring(unittest.IsolatedAsyncioTestCase):
    async def test_send_coo_approval_renders_embed_and_view(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from gateway.config import PlatformConfig
        from plugins.platforms.discord.adapter import DiscordAdapter

        adapter = DiscordAdapter(PlatformConfig(enabled=True, token="token"))
        sent_msg = SimpleNamespace(id=4242)
        channel = SimpleNamespace(send=AsyncMock(return_value=sent_msg))
        adapter._client = SimpleNamespace(
            get_channel=lambda _cid: channel,
            fetch_channel=AsyncMock(),
        )

        session = _sample_session_payload()
        fake_embed = object()
        fake_view = object()

        with patch("plugins.platforms.discord.adapter.DISCORD_AVAILABLE", True), patch.object(
            coo_approval,
            "prepare_coo_approval_render_items",
            return_value=(fake_embed, fake_view),
        ) as mock_prepare, patch(
            "agent.coo.discord_approval_adapter.approve_discord_session"
        ) as mock_approve, patch(
            "agent.coo.discord_approval_adapter.reject_discord_session"
        ) as mock_reject, patch(
            "tools.approval.resolve_gateway_approval"
        ) as mock_exec_approval:
            result = await adapter.send_coo_approval("555", session)

        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "4242")
        mock_prepare.assert_called_once_with(session, store=None)
        channel.send.assert_awaited_once()
        kwargs = channel.send.await_args.kwargs
        self.assertIs(kwargs["embed"], fake_embed)
        self.assertIs(kwargs["view"], fake_view)
        mock_approve.assert_not_called()
        mock_reject.assert_not_called()
        mock_exec_approval.assert_not_called()

    async def test_send_coo_approval_rejects_missing_payload(self) -> None:
        from gateway.config import PlatformConfig
        from plugins.platforms.discord.adapter import DiscordAdapter

        adapter = DiscordAdapter(PlatformConfig(enabled=True, token="token"))
        adapter._client = object()

        with patch("plugins.platforms.discord.adapter.DISCORD_AVAILABLE", True):
            result = await adapter.send_coo_approval("555", {})

        self.assertFalse(result.success)
        self.assertIn("Missing COO approval session payload", result.error or "")

    def test_send_coo_approval_wiring_does_not_modify_exec_approval(self) -> None:
        import inspect

        from plugins.platforms.discord.adapter import DiscordAdapter

        exec_source = inspect.getsource(DiscordAdapter.send_exec_approval)
        coo_source = inspect.getsource(DiscordAdapter.send_coo_approval)

        self.assertIn("ExecApprovalView", exec_source)
        self.assertIn("resolve_gateway_approval", exec_source)
        self.assertIn("prepare_coo_approval_render_items", coo_source)
        self.assertNotIn("from tools.approval import resolve_gateway_approval", coo_source)
        self.assertNotIn("resolve_gateway_approval(", coo_source)
        self.assertNotIn("ExecApprovalView", coo_source)
        self.assertNotIn("approve_discord_session", coo_source)


class TestDiscordCooApprovalCustomIdParse(unittest.TestCase):
    def test_parse_valid_custom_id(self) -> None:
        session_id = "11111111-2222-3333-4444-555555555555"
        parsed = parse_coo_approval_custom_id(f"coo_approval:approve:{session_id}")

        self.assertEqual(parsed["prefix"], "coo_approval")
        self.assertEqual(parsed["action"], "approve")
        self.assertEqual(parsed["session_id"], session_id)

    def test_parse_rejects_invalid_custom_id(self) -> None:
        with self.assertRaises(ValueError):
            parse_coo_approval_custom_id("exec:approve:123")
        with self.assertRaises(ValueError):
            parse_coo_approval_custom_id("coo_approval:launch:123")
        with self.assertRaises(ValueError):
            parse_coo_approval_custom_id("coo_approval:approve:")


class TestDiscordCooApprovalButtonActions(unittest.TestCase):
    def test_execute_approve_updates_session_without_execution(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        result = execute_coo_approval_button_action(
            action="approve",
            session_id=seeded["session_id"],
            discord_user_id="987654321012345678",
            store=store,
        )

        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["execution_ticket_id"], "")
        self.assertFalse(result["execution_dispatched"])
        self.assertFalse(result["publish_dispatched"])

    def test_execute_reject_updates_session(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        result = execute_coo_approval_button_action(
            action="reject",
            session_id=seeded["session_id"],
            discord_user_id="987654321012345678",
            store=store,
        )

        self.assertEqual(result["status"], "rejected")

    def test_execute_refresh_returns_session_snapshot(self) -> None:
        from agent.coo.discord_approval_adapter import get_discord_approval_session

        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        result = execute_coo_approval_button_action(
            action="refresh",
            session_id=seeded["session_id"],
            discord_user_id="987654321012345678",
            store=store,
        )

        self.assertEqual(result["session_id"], seeded["session_id"])
        self.assertEqual(
            result,
            get_discord_approval_session(seeded["session_id"], store=store),
        )

    def test_execute_approve_rejects_wrong_requester(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)

        with self.assertRaises(ValueError):
            execute_coo_approval_button_action(
                action="approve",
                session_id=seeded["session_id"],
                discord_user_id="000000000000000001",
                store=store,
            )

        self.assertEqual(
            coo_approval_error_message(ValueError("Requester '1' is not authorized")),
            _ERR_NOT_ALLOWED,
        )

    def test_execute_missing_session_raises_key_error(self) -> None:
        store = CEOApprovalSessionStore()

        with self.assertRaises(KeyError):
            execute_coo_approval_button_action(
                action="refresh",
                session_id="missing-session",
                discord_user_id="987654321012345678",
                store=store,
            )

        self.assertEqual(coo_approval_error_message(KeyError("missing")), _ERR_SESSION_NOT_FOUND)


class TestDiscordCooApprovalButtonCallbacks(unittest.IsolatedAsyncioTestCase):
    def _mock_interaction(self, user_id: int = 987654321012345678):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            response=SimpleNamespace(
                is_done=lambda: False,
                send_message=AsyncMock(),
                edit_message=AsyncMock(),
            ),
            message=SimpleNamespace(edit=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_approve_callback_calls_adapter_and_updates_embed(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        custom_id = f"coo_approval:approve:{seeded['session_id']}"
        callback = _make_coo_approval_button_callback(custom_id, store=store)
        interaction = self._mock_interaction()
        fake_discord = _make_fake_discord_module()

        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord), patch(
            "agent.coo.discord_approval_adapter.approve_discord_session",
            wraps=__import__(
                "agent.coo.discord_approval_adapter",
                fromlist=["approve_discord_session"],
            ).approve_discord_session,
        ) as mock_approve, patch(
            "tools.approval.resolve_gateway_approval"
        ) as mock_exec_approval:
            await callback(interaction)

        mock_approve.assert_called_once()
        mock_exec_approval.assert_not_called()
        interaction.response.edit_message.assert_awaited()

    async def test_reject_callback_calls_adapter(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        custom_id = f"coo_approval:reject:{seeded['session_id']}"
        callback = _make_coo_approval_button_callback(custom_id, store=store)
        interaction = self._mock_interaction()
        fake_discord = _make_fake_discord_module()

        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord), patch(
            "agent.coo.discord_approval_adapter.reject_discord_session",
            wraps=__import__(
                "agent.coo.discord_approval_adapter",
                fromlist=["reject_discord_session"],
            ).reject_discord_session,
        ) as mock_reject:
            await callback(interaction)

        mock_reject.assert_called_once()

    async def test_refresh_callback_calls_get_session(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        custom_id = f"coo_approval:refresh:{seeded['session_id']}"
        callback = _make_coo_approval_button_callback(custom_id, store=store)
        interaction = self._mock_interaction()
        fake_discord = _make_fake_discord_module()

        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord), patch(
            "agent.coo.discord_approval_adapter.get_discord_approval_session",
            wraps=__import__(
                "agent.coo.discord_approval_adapter",
                fromlist=["get_discord_approval_session"],
            ).get_discord_approval_session,
        ) as mock_get:
            await callback(interaction)

        mock_get.assert_called()

    async def test_wrong_requester_gets_ephemeral_error(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        custom_id = f"coo_approval:approve:{seeded['session_id']}"
        callback = _make_coo_approval_button_callback(custom_id, store=store)
        interaction = self._mock_interaction(user_id=1)

        await callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        args, kwargs = interaction.response.send_message.await_args
        self.assertEqual(args[0], _ERR_NOT_ALLOWED)
        self.assertTrue(kwargs.get("ephemeral"))


class TestDiscordCooApprovalButtonDisablePolicy(unittest.TestCase):
    def test_is_terminal_approval_status(self) -> None:
        for status in ("approved", "rejected", "expired", "cancelled", "APPROVED"):
            self.assertTrue(_is_terminal_approval_status(status))
        self.assertFalse(_is_terminal_approval_status("pending"))
        self.assertFalse(_is_terminal_approval_status(""))

    def test_pending_components_not_disabled(self) -> None:
        components = build_coo_approval_components(_sample_session_payload(status="pending"))
        for button in components:
            self.assertFalse(button.get("disabled", False))

    def test_approved_components_disabled(self) -> None:
        components = build_coo_approval_components(_sample_session_payload(status="approved"))
        self.assertTrue(_should_disable_coo_approval_buttons(_sample_session_payload(status="approved")))
        self.assertTrue(all(button.get("disabled") for button in components))

    def test_rejected_components_disabled(self) -> None:
        components = build_coo_approval_components(_sample_session_payload(status="rejected"))
        self.assertTrue(all(button.get("disabled") for button in components))

    def test_expired_components_disabled(self) -> None:
        components = build_coo_approval_components(_sample_session_payload(status="expired"))
        self.assertTrue(all(button.get("disabled") for button in components))

    def test_cancelled_components_disabled(self) -> None:
        components = build_coo_approval_components(_sample_session_payload(status="cancelled"))
        self.assertTrue(all(button.get("disabled") for button in components))

    def test_view_builder_applies_disabled_flag(self) -> None:
        components = build_coo_approval_components(_sample_session_payload(status="approved"))
        fake_discord = _make_fake_discord_module()
        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord):
            view = build_discord_view_from_components(components)

        self.assertTrue(all(button.disabled for button in view.children))

    def test_execute_approve_rejects_terminal_session(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store, status="approved")

        with self.assertRaises(ValueError):
            execute_coo_approval_button_action(
                action="approve",
                session_id=seeded["session_id"],
                discord_user_id="987654321012345678",
                store=store,
            )


class TestDiscordCooApprovalStoreInjection(unittest.IsolatedAsyncioTestCase):
    async def test_send_coo_approval_forwards_custom_store(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from gateway.config import PlatformConfig
        from plugins.platforms.discord.adapter import DiscordAdapter

        adapter = DiscordAdapter(PlatformConfig(enabled=True, token="token"))
        sent_msg = SimpleNamespace(id=4242)
        channel = SimpleNamespace(send=AsyncMock(return_value=sent_msg))
        adapter._client = SimpleNamespace(
            get_channel=lambda _cid: channel,
            fetch_channel=AsyncMock(),
        )

        session = _sample_session_payload()
        custom_store = CEOApprovalSessionStore()
        fake_embed = object()
        fake_view = object()

        with patch("plugins.platforms.discord.adapter.DISCORD_AVAILABLE", True), patch.object(
            coo_approval,
            "prepare_coo_approval_render_items",
            return_value=(fake_embed, fake_view),
        ) as mock_prepare:
            result = await adapter.send_coo_approval("555", session, store=custom_store)

        self.assertTrue(result.success)
        mock_prepare.assert_called_once_with(session, store=custom_store)

    def test_prepare_render_items_propagates_store_to_view(self) -> None:
        store = CEOApprovalSessionStore()
        session = _sample_session_payload()
        fake_discord = _make_fake_discord_module()

        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord):
            _embed, view = prepare_coo_approval_render_items(session, store=store)

        self.assertEqual(len(view.children), 3)
        for button in view.children:
            self.assertTrue(callable(button.callback))

    def test_store_none_uses_default_adapter_store(self) -> None:
        from agent.coo.approval_session import get_default_session_store

        store = get_default_session_store()
        store.clear()
        seeded = _seed_session_in_store(store)
        result = execute_coo_approval_button_action(
            action="refresh",
            session_id=seeded["session_id"],
            discord_user_id="987654321012345678",
            store=None,
        )

        self.assertEqual(result["session_id"], seeded["session_id"])
        store.clear()


class TestDiscordCooApprovalInteractionViewRefresh(unittest.IsolatedAsyncioTestCase):
    def _mock_interaction(self, user_id: int = 987654321012345678):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            response=SimpleNamespace(
                is_done=lambda: False,
                send_message=AsyncMock(),
                edit_message=AsyncMock(),
            ),
            message=SimpleNamespace(edit=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_approve_callback_updates_view_with_disabled_buttons(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        custom_id = f"coo_approval:approve:{seeded['session_id']}"
        callback = _make_coo_approval_button_callback(custom_id, store=store)
        interaction = self._mock_interaction()
        fake_discord = _make_fake_discord_module()

        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord):
            await callback(interaction)

        edit_kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertIn("view", edit_kwargs)
        self.assertIn("embed", edit_kwargs)
        self.assertTrue(all(button.disabled for button in edit_kwargs["view"].children))

    async def test_reject_callback_updates_view_with_disabled_buttons(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        custom_id = f"coo_approval:reject:{seeded['session_id']}"
        callback = _make_coo_approval_button_callback(custom_id, store=store)
        interaction = self._mock_interaction()
        fake_discord = _make_fake_discord_module()

        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord):
            await callback(interaction)

        edit_kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertIn("view", edit_kwargs)
        self.assertTrue(all(button.disabled for button in edit_kwargs["view"].children))

    async def test_refresh_callback_reflects_terminal_disabled_buttons(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store, status="approved")
        custom_id = f"coo_approval:refresh:{seeded['session_id']}"
        callback = _make_coo_approval_button_callback(custom_id, store=store)
        interaction = self._mock_interaction()
        fake_discord = _make_fake_discord_module()

        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord):
            await callback(interaction)

        edit_kwargs = interaction.response.edit_message.await_args.kwargs
        self.assertTrue(all(button.disabled for button in edit_kwargs["view"].children))

    async def test_terminal_reclick_does_not_mutate_session(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store, status="approved")
        custom_id = f"coo_approval:approve:{seeded['session_id']}"
        callback = _make_coo_approval_button_callback(custom_id, store=store)
        interaction = self._mock_interaction()
        fake_discord = _make_fake_discord_module()

        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord), patch(
            "agent.coo.discord_approval_adapter.approve_discord_session"
        ) as mock_approve, patch(
            "tools.approval.resolve_gateway_approval"
        ) as mock_exec_approval:
            await callback(interaction)

        mock_approve.assert_not_called()
        mock_exec_approval.assert_not_called()
        interaction.response.send_message.assert_awaited_once()
        args, kwargs = interaction.response.send_message.await_args
        self.assertIn("already approved", args[0].lower())
        self.assertTrue(kwargs.get("ephemeral"))

    async def test_approve_callback_preserves_execution_block(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        custom_id = f"coo_approval:approve:{seeded['session_id']}"
        callback = _make_coo_approval_button_callback(custom_id, store=store)
        interaction = self._mock_interaction()
        fake_discord = _make_fake_discord_module()

        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord), patch(
            "tools.approval.resolve_gateway_approval"
        ) as mock_exec_approval:
            await callback(interaction)

        mock_exec_approval.assert_not_called()
        updated = store.get(seeded["session_id"])
        assert updated is not None
        self.assertEqual(updated.execution_ticket_id, "")
        self.assertFalse(updated.execution_dispatched)
        self.assertFalse(updated.publish_dispatched)


if __name__ == "__main__":
    unittest.main()
