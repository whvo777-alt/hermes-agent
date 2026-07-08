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
    _should_disable_dry_run_preview_button,
    _should_disable_prepare_plan_button,
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

    def test_components_include_all_buttons_with_prefix(self) -> None:
        session = _sample_session_payload()
        components = build_coo_approval_components(session)

        self.assertEqual(len(components), 5)
        labels = [button["label"] for button in components]
        self.assertEqual(
            labels,
            ["Approve", "Reject", "Refresh", "Prepare Plan", "Dry Run Preview"],
        )
        for button in components:
            self.assertTrue(button["custom_id"].startswith("coo_approval:"))
            self.assertIn(session["session_id"], button["custom_id"])
            label_lower = button["label"].lower()
            self.assertNotIn("execute", label_lower)
            self.assertNotIn("dispatch", label_lower)

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
        self.assertEqual(
            components[3]["custom_id"],
            f"coo_approval:prepare_plan:{session['session_id']}",
        )
        self.assertEqual(
            components[4]["custom_id"],
            f"coo_approval:dry_run_preview:{session['session_id']}",
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
        self.assertEqual(len(result.get("components", [])), 5)
        self.assertEqual(result["components"][0]["custom_id"], components[0]["custom_id"])

    def test_build_discord_view_preserves_custom_ids_with_fake_discord(self) -> None:
        components = build_coo_approval_components(_sample_session_payload())
        fake_discord = _make_fake_discord_module()
        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord):
            view = build_discord_view_from_components(components)

        self.assertEqual(len(view.children), 5)
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

    def test_parse_prepare_plan_custom_id(self) -> None:
        session_id = "11111111-2222-3333-4444-555555555555"
        parsed = parse_coo_approval_custom_id(f"coo_approval:prepare_plan:{session_id}")

        self.assertEqual(parsed["prefix"], "coo_approval")
        self.assertEqual(parsed["action"], "prepare_plan")
        self.assertEqual(parsed["session_id"], session_id)

    def test_parse_dry_run_preview_custom_id(self) -> None:
        session_id = "11111111-2222-3333-4444-555555555555"
        parsed = parse_coo_approval_custom_id(f"coo_approval:dry_run_preview:{session_id}")

        self.assertEqual(parsed["prefix"], "coo_approval")
        self.assertEqual(parsed["action"], "dry_run_preview")
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
        self.assertTrue(result["execution_ticket_id"])
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

    def test_pending_components_not_disabled_except_prepare_plan(self) -> None:
        components = build_coo_approval_components(_sample_session_payload(status="pending"))
        by_label = {button["label"]: button for button in components}
        for label in ("Approve", "Reject", "Refresh"):
            self.assertFalse(by_label[label].get("disabled", False))
        self.assertTrue(by_label["Prepare Plan"].get("disabled", False))

    def test_approved_components_disable_approve_reject_refresh_only(self) -> None:
        session = _sample_session_payload(
            status="approved",
            execution_ticket_id="ticket-abc",
        )
        components = build_coo_approval_components(session)
        by_label = {button["label"]: button for button in components}

        self.assertTrue(_should_disable_coo_approval_buttons(session))
        self.assertFalse(_should_disable_prepare_plan_button(session))
        self.assertTrue(by_label["Approve"]["disabled"])
        self.assertTrue(by_label["Reject"]["disabled"])
        self.assertTrue(by_label["Refresh"]["disabled"])
        self.assertFalse(by_label["Prepare Plan"].get("disabled", False))
        self.assertTrue(by_label["Dry Run Preview"].get("disabled", False))

    def test_approved_with_ticket_and_plan_enables_dry_run_preview(self) -> None:
        session = _sample_session_payload(
            status="approved",
            execution_ticket_id="ticket-abc",
        )
        plan = {
            "dispatchable_skills": ["create_content"],
            "preview_only_skills": ["approval_review"],
            "excluded_skills": ["publish_content"],
        }
        with patch.object(
            coo_approval,
            "_lookup_dispatch_plan_for_session",
            return_value=plan,
        ):
            components = build_coo_approval_components(session)
            by_label = {button["label"]: button for button in components}
            self.assertFalse(_should_disable_dry_run_preview_button(session))

        self.assertFalse(by_label["Dry Run Preview"].get("disabled", False))

    def test_approved_without_plan_disables_dry_run_preview(self) -> None:
        session = _sample_session_payload(
            status="approved",
            execution_ticket_id="ticket-abc",
        )
        with patch.object(
            coo_approval,
            "_lookup_dispatch_plan_for_session",
            return_value=None,
        ):
            components = build_coo_approval_components(session)
            by_label = {button["label"]: button for button in components}

        self.assertTrue(_should_disable_dry_run_preview_button(session))
        self.assertTrue(by_label["Dry Run Preview"].get("disabled", False))

    def test_approved_without_ticket_disables_prepare_plan(self) -> None:
        components = build_coo_approval_components(
            _sample_session_payload(status="approved", execution_ticket_id="")
        )
        by_label = {button["label"]: button for button in components}

        self.assertTrue(_should_disable_prepare_plan_button(_sample_session_payload(status="approved")))
        self.assertTrue(by_label["Prepare Plan"]["disabled"])
        self.assertTrue(by_label["Dry Run Preview"].get("disabled", False))

    def test_pending_prepare_plan_disabled(self) -> None:
        components = build_coo_approval_components(_sample_session_payload(status="pending"))
        by_label = {button["label"]: button for button in components}

        self.assertTrue(_should_disable_prepare_plan_button(_sample_session_payload(status="pending")))
        self.assertTrue(by_label["Prepare Plan"]["disabled"])
        self.assertTrue(by_label["Dry Run Preview"].get("disabled", False))

    def test_rejected_components_disabled(self) -> None:
        components = build_coo_approval_components(_sample_session_payload(status="rejected"))
        self.assertTrue(all(button.get("disabled") for button in components))

    def test_rejected_prepare_plan_disabled(self) -> None:
        components = build_coo_approval_components(
            _sample_session_payload(status="rejected", execution_ticket_id="ticket-abc")
        )
        self.assertTrue(all(button.get("disabled") for button in components))

    def test_expired_components_disabled(self) -> None:
        components = build_coo_approval_components(_sample_session_payload(status="expired"))
        self.assertTrue(all(button.get("disabled") for button in components))

    def test_expired_prepare_plan_disabled(self) -> None:
        components = build_coo_approval_components(
            _sample_session_payload(status="expired", execution_ticket_id="ticket-abc")
        )
        self.assertTrue(all(button.get("disabled") for button in components))

    def test_cancelled_components_disabled(self) -> None:
        components = build_coo_approval_components(_sample_session_payload(status="cancelled"))
        self.assertTrue(all(button.get("disabled") for button in components))

    def test_cancelled_prepare_plan_disabled(self) -> None:
        components = build_coo_approval_components(
            _sample_session_payload(status="cancelled", execution_ticket_id="ticket-abc")
        )
        self.assertTrue(all(button.get("disabled") for button in components))

    def test_view_builder_applies_split_disabled_flags(self) -> None:
        components = build_coo_approval_components(
            _sample_session_payload(status="approved", execution_ticket_id="ticket-abc")
        )
        fake_discord = _make_fake_discord_module()
        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord):
            view = build_discord_view_from_components(components)

        by_label = {button.label: button for button in view.children}
        self.assertTrue(by_label["Approve"].disabled)
        self.assertTrue(by_label["Reject"].disabled)
        self.assertTrue(by_label["Refresh"].disabled)
        self.assertFalse(by_label["Prepare Plan"].disabled)

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

        self.assertEqual(len(view.children), 5)
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
        by_label = {button.label: button for button in edit_kwargs["view"].children}
        self.assertTrue(by_label["Approve"].disabled)
        self.assertTrue(by_label["Reject"].disabled)
        self.assertTrue(by_label["Refresh"].disabled)
        self.assertFalse(by_label["Prepare Plan"].disabled)
        self.assertTrue(by_label["Dry Run Preview"].disabled)

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
        self.assertTrue(updated.execution_ticket_id)
        self.assertFalse(updated.execution_dispatched)
        self.assertFalse(updated.publish_dispatched)


class TestDiscordCooApprovalPreparePlan(unittest.TestCase):
    def setUp(self) -> None:
        from agent.coo.execution_dispatcher import get_default_dispatch_plan_store
        from agent.coo.execution_ticket import get_default_ticket_store

        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()

    def test_embed_shows_plan_fields_when_plan_payload_provided(self) -> None:
        plan = {
            "dispatchable_skills": ["create_content"],
            "preview_only_skills": ["approval_review"],
            "excluded_skills": ["publish_content"],
            "exclusion_reasons": {"publish_content": "publish skill excluded"},
            "requested_by": "987654321012345678",
            "requested_at": "2026-07-08T00:00:00Z",
        }
        session = _sample_session_payload(
            status="approved",
            execution_ticket_id="ticket-abc",
        )
        embed = build_coo_approval_embed_payload(session, plan_payload=plan)
        field_map = {field["name"]: field["value"] for field in embed["fields"]}

        self.assertEqual(field_map["Plan Status"], "Plan Ready — Not Executed")
        self.assertIn("create_content", field_map["Dispatchable"])
        self.assertIn("approval_review", field_map["Preview Only"])
        self.assertIn("publish_content", field_map["Excluded"])
        self.assertEqual(field_map["Requested By"], "987654321012345678")
        self.assertEqual(field_map["Requested At"], "2026-07-08T00:00:00Z")
        self.assertEqual(field_map["Execution"], "Not dispatched")
        self.assertEqual(field_map["Publish"], "Not dispatched")
        self.assertIn("Plan only", embed["footer"]["text"])

    def test_embed_lookup_failure_does_not_break_ui(self) -> None:
        session = _sample_session_payload(
            status="approved",
            execution_ticket_id="ticket-abc",
        )
        with patch(
            "agent.coo.gateway_execution_dispatcher.get_dispatch_plan_for_gateway_ticket",
            side_effect=RuntimeError("lookup failed"),
        ):
            embed = build_coo_approval_embed_payload(session)

        field_map = {field["name"]: field["value"] for field in embed["fields"]}
        self.assertNotIn("Plan Status", field_map)
        self.assertIn("Approval only", embed["footer"]["text"])

    def test_execute_prepare_plan_calls_gateway_bridge(self) -> None:
        import subprocess

        from agent.coo.gateway_execution_dispatcher import (
            create_dispatch_plan_for_gateway_session,
        )
        from agent.coo.gateway_approval import approve_gateway_session

        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            approved = approve_gateway_session(
                seeded["session_id"],
                reviewer="987654321012345678",
                requester_id="987654321012345678",
                store=store,
            )

        with patch.object(
            subprocess,
            "run",
            side_effect=AssertionError("no subprocess"),
        ), patch(
            "agent.coo.gateway_execution_dispatcher.create_dispatch_plan_for_gateway_session",
            wraps=create_dispatch_plan_for_gateway_session,
        ) as mock_create_plan:
            result = execute_coo_approval_button_action(
                action="prepare_plan",
                session_id=approved["session_id"],
                discord_user_id="987654321012345678",
                store=store,
            )

        mock_create_plan.assert_called_once_with(
            approved["session_id"],
            requester_id="987654321012345678",
            reason="discord prepare plan",
        )
        self.assertEqual(result["status"], "approved")
        self.assertTrue(result["execution_ticket_id"])
        self.assertFalse(result["execution_dispatched"])
        self.assertFalse(result["publish_dispatched"])

    def test_execute_prepare_plan_rejects_wrong_requester(self) -> None:
        import subprocess

        from agent.coo.gateway_approval import approve_gateway_session

        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            approved = approve_gateway_session(
                seeded["session_id"],
                reviewer="987654321012345678",
                requester_id="987654321012345678",
                store=store,
            )

        with patch(
            "agent.coo.gateway_execution_dispatcher.create_dispatch_plan_for_gateway_session"
        ) as mock_create_plan, self.assertRaises(ValueError):
            execute_coo_approval_button_action(
                action="prepare_plan",
                session_id=approved["session_id"],
                discord_user_id="000000000000000001",
                store=store,
            )

        mock_create_plan.assert_not_called()

    def test_execute_approve_does_not_call_plan_bridge(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)

        with patch(
            "agent.coo.gateway_execution_dispatcher.create_dispatch_plan_for_gateway_session"
        ) as mock_create_plan:
            execute_coo_approval_button_action(
                action="approve",
                session_id=seeded["session_id"],
                discord_user_id="987654321012345678",
                store=store,
            )

        mock_create_plan.assert_not_called()


class TestDiscordCooApprovalPreparePlanCallbacks(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from agent.coo.execution_dispatcher import get_default_dispatch_plan_store
        from agent.coo.execution_ticket import get_default_ticket_store

        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()

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

    async def test_prepare_plan_callback_sends_ephemeral_and_updates_embed(self) -> None:
        import subprocess

        from agent.coo.gateway_approval import approve_gateway_session

        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            approved = approve_gateway_session(
                seeded["session_id"],
                reviewer="987654321012345678",
                requester_id="987654321012345678",
                store=store,
            )

        custom_id = f"coo_approval:prepare_plan:{approved['session_id']}"
        callback = _make_coo_approval_button_callback(custom_id, store=store)
        interaction = self._mock_interaction()
        fake_discord = _make_fake_discord_module()

        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord), patch.object(
            subprocess,
            "run",
            side_effect=AssertionError("no subprocess"),
        ), patch(
            "agent.coo.gateway_execution_dispatcher.create_dispatch_plan_for_gateway_session",
            wraps=__import__(
                "agent.coo.gateway_execution_dispatcher",
                fromlist=["create_dispatch_plan_for_gateway_session"],
            ).create_dispatch_plan_for_gateway_session,
        ) as mock_create_plan:
            await callback(interaction)

        mock_create_plan.assert_called_once()
        interaction.response.edit_message.assert_awaited()
        edit_kwargs = interaction.response.edit_message.await_args.kwargs
        embed_payload = edit_kwargs["embed"]
        if hasattr(embed_payload, "fields"):
            field_map = {
                (field.name if hasattr(field, "name") else field["name"]): (
                    field.value if hasattr(field, "value") else field["value"]
                )
                for field in embed_payload.fields
            }
        else:
            field_map = {field["name"]: field["value"] for field in embed_payload["fields"]}
        self.assertEqual(field_map["Plan Status"], "Plan Ready — Not Executed")
        self.assertEqual(field_map["Execution"], "Not dispatched")
        self.assertEqual(field_map["Publish"], "Not dispatched")
        interaction.response.send_message.assert_awaited_once()
        args, kwargs = interaction.response.send_message.await_args
        self.assertEqual(args[0], "Plan Ready — Not Executed")
        self.assertTrue(kwargs.get("ephemeral"))

    async def test_prepare_plan_wrong_requester_gets_ephemeral_error(self) -> None:
        import subprocess

        from agent.coo.gateway_approval import approve_gateway_session

        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            approved = approve_gateway_session(
                seeded["session_id"],
                reviewer="987654321012345678",
                requester_id="987654321012345678",
                store=store,
            )

        custom_id = f"coo_approval:prepare_plan:{approved['session_id']}"
        callback = _make_coo_approval_button_callback(custom_id, store=store)
        interaction = self._mock_interaction(user_id=1)

        with patch(
            "agent.coo.gateway_execution_dispatcher.create_dispatch_plan_for_gateway_session"
        ) as mock_create_plan:
            await callback(interaction)

        mock_create_plan.assert_not_called()
        interaction.response.send_message.assert_awaited_once()
        args, kwargs = interaction.response.send_message.await_args
        self.assertEqual(args[0], _ERR_NOT_ALLOWED)
        self.assertTrue(kwargs.get("ephemeral"))


class TestDiscordCooApprovalDryRunPreview(unittest.TestCase):
    def setUp(self) -> None:
        from agent.coo.execution_dispatcher import get_default_dispatch_plan_store
        from agent.coo.execution_runtime import get_default_execution_run_store
        from agent.coo.execution_ticket import get_default_ticket_store

        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()
        get_default_execution_run_store().clear()

    def test_embed_shows_run_fields_when_run_payload_provided(self) -> None:
        run = {
            "dispatchable_results": [
                {"skill_id": "create_content", "dry_run": True, "status": "planned"}
            ],
            "preview_results": [
                {"skill_id": "approval_review", "dry_run": True, "status": "preview_planned"}
            ],
            "blocked_skills": ["publish_content"],
            "summary": "1 dispatchable skill(s) dry-run planned",
            "finished_at": "2026-07-08T12:00:00Z",
        }
        session = _sample_session_payload(
            status="approved",
            execution_ticket_id="ticket-abc",
        )
        embed = build_coo_approval_embed_payload(session, run_payload=run)
        field_map = {field["name"]: field["value"] for field in embed["fields"]}

        self.assertEqual(field_map["Dry Run Status"], "Dry Run Preview — Not Executed")
        self.assertIn("create_content", field_map["Dispatchable Results"])
        self.assertIn("approval_review", field_map["Preview Results"])
        self.assertIn("publish_content", field_map["Blocked Skills"])
        self.assertIn("dry-run planned", field_map["Run Summary"])
        self.assertEqual(field_map["Run At"], "2026-07-08T12:00:00Z")
        self.assertEqual(field_map["Execution"], "Not dispatched")
        self.assertEqual(field_map["Publish"], "Not dispatched")
        self.assertIn("Dry run preview only", embed["footer"]["text"])

    def test_execute_dry_run_preview_calls_gateway_runtime_bridge(self) -> None:
        import subprocess

        from agent.coo.execution_dispatcher import get_default_dispatch_plan_store
        from agent.coo.execution_runtime import get_default_execution_run_store
        from agent.coo.execution_ticket import get_default_ticket_store
        from agent.coo.gateway_execution_dispatcher import create_dispatch_plan_for_gateway_session
        from agent.coo.gateway_execution_runtime import start_dry_run_for_gateway_session
        from agent.coo.gateway_approval import approve_gateway_session

        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()
        get_default_execution_run_store().clear()

        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            approved = approve_gateway_session(
                seeded["session_id"],
                reviewer="987654321012345678",
                requester_id="987654321012345678",
                store=store,
            )
            create_dispatch_plan_for_gateway_session(
                approved["session_id"],
                requester_id="987654321012345678",
            )

        with patch.object(
            subprocess,
            "run",
            side_effect=AssertionError("no subprocess"),
        ), patch(
            "agent.coo.gateway_execution_runtime.start_dry_run_for_gateway_session",
            wraps=start_dry_run_for_gateway_session,
        ) as mock_start_dry_run:
            result = execute_coo_approval_button_action(
                action="dry_run_preview",
                session_id=approved["session_id"],
                discord_user_id="987654321012345678",
                store=store,
            )

        mock_start_dry_run.assert_called_once_with(
            approved["session_id"],
            requester_id="987654321012345678",
            reason="discord dry run preview",
        )
        self.assertEqual(result["status"], "approved")
        self.assertFalse(result["execution_dispatched"])
        self.assertFalse(result["publish_dispatched"])

    def test_execute_dry_run_preview_rejects_wrong_requester(self) -> None:
        import subprocess

        from agent.coo.execution_dispatcher import get_default_dispatch_plan_store
        from agent.coo.execution_ticket import get_default_ticket_store
        from agent.coo.gateway_execution_dispatcher import create_dispatch_plan_for_gateway_session
        from agent.coo.gateway_approval import approve_gateway_session

        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()

        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            approved = approve_gateway_session(
                seeded["session_id"],
                reviewer="987654321012345678",
                requester_id="987654321012345678",
                store=store,
            )
            create_dispatch_plan_for_gateway_session(
                approved["session_id"],
                requester_id="987654321012345678",
            )

        with patch(
            "agent.coo.gateway_execution_runtime.start_dry_run_for_gateway_session"
        ) as mock_start_dry_run, self.assertRaises(ValueError):
            execute_coo_approval_button_action(
                action="dry_run_preview",
                session_id=approved["session_id"],
                discord_user_id="000000000000000001",
                store=store,
            )

        mock_start_dry_run.assert_not_called()

    def test_execute_approve_does_not_call_dry_run_bridge(self) -> None:
        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)

        with patch(
            "agent.coo.gateway_execution_runtime.start_dry_run_for_gateway_session"
        ) as mock_start_dry_run:
            execute_coo_approval_button_action(
                action="approve",
                session_id=seeded["session_id"],
                discord_user_id="987654321012345678",
                store=store,
            )

        mock_start_dry_run.assert_not_called()

    def test_execute_prepare_plan_does_not_call_dry_run_bridge(self) -> None:
        import subprocess

        from agent.coo.gateway_approval import approve_gateway_session

        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            approved = approve_gateway_session(
                seeded["session_id"],
                reviewer="987654321012345678",
                requester_id="987654321012345678",
                store=store,
            )

        with patch(
            "agent.coo.gateway_execution_runtime.start_dry_run_for_gateway_session"
        ) as mock_start_dry_run:
            execute_coo_approval_button_action(
                action="prepare_plan",
                session_id=approved["session_id"],
                discord_user_id="987654321012345678",
                store=store,
            )

        mock_start_dry_run.assert_not_called()

    def test_subprocess_not_called_for_dry_run_preview(self) -> None:
        import subprocess

        from agent.coo.execution_dispatcher import get_default_dispatch_plan_store
        from agent.coo.execution_runtime import get_default_execution_run_store
        from agent.coo.execution_ticket import get_default_ticket_store
        from agent.coo.gateway_execution_dispatcher import create_dispatch_plan_for_gateway_session
        from agent.coo.gateway_approval import approve_gateway_session

        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()
        get_default_execution_run_store().clear()

        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            approved = approve_gateway_session(
                seeded["session_id"],
                reviewer="987654321012345678",
                requester_id="987654321012345678",
                store=store,
            )
            create_dispatch_plan_for_gateway_session(
                approved["session_id"],
                requester_id="987654321012345678",
            )
            execute_coo_approval_button_action(
                action="dry_run_preview",
                session_id=approved["session_id"],
                discord_user_id="987654321012345678",
                store=store,
            )

    def test_gateway_execution_runtime_lazy_import_only(self) -> None:
        from pathlib import Path

        discord_path = (
            Path(__file__).resolve().parents[2]
            / "plugins/platforms/discord/coo_approval.py"
        )
        source = discord_path.read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith("from agent.coo.gateway_execution_runtime import"):
                self.fail(
                    "gateway_execution_runtime must be imported lazily inside functions"
                )
            if line.startswith("import agent.coo.gateway_execution_runtime"):
                self.fail(
                    "gateway_execution_runtime must be imported lazily inside functions"
                )


class TestDiscordCooApprovalDryRunPreviewCallbacks(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from agent.coo.execution_dispatcher import get_default_dispatch_plan_store
        from agent.coo.execution_runtime import get_default_execution_run_store
        from agent.coo.execution_ticket import get_default_ticket_store

        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()
        get_default_execution_run_store().clear()

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

    async def test_dry_run_preview_callback_sends_ephemeral_and_updates_embed(self) -> None:
        import subprocess

        from agent.coo.execution_dispatcher import get_default_dispatch_plan_store
        from agent.coo.execution_runtime import get_default_execution_run_store
        from agent.coo.execution_ticket import get_default_ticket_store
        from agent.coo.gateway_execution_dispatcher import create_dispatch_plan_for_gateway_session
        from agent.coo.gateway_approval import approve_gateway_session

        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()
        get_default_execution_run_store().clear()

        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            approved = approve_gateway_session(
                seeded["session_id"],
                reviewer="987654321012345678",
                requester_id="987654321012345678",
                store=store,
            )
            create_dispatch_plan_for_gateway_session(
                approved["session_id"],
                requester_id="987654321012345678",
            )

        custom_id = f"coo_approval:dry_run_preview:{approved['session_id']}"
        callback = _make_coo_approval_button_callback(custom_id, store=store)
        interaction = self._mock_interaction()
        fake_discord = _make_fake_discord_module()

        with patch.object(coo_approval, "_get_discord_module", return_value=fake_discord), patch.object(
            subprocess,
            "run",
            side_effect=AssertionError("no subprocess"),
        ), patch(
            "agent.coo.gateway_execution_runtime.start_dry_run_for_gateway_session",
            wraps=__import__(
                "agent.coo.gateway_execution_runtime",
                fromlist=["start_dry_run_for_gateway_session"],
            ).start_dry_run_for_gateway_session,
        ) as mock_start_dry_run:
            await callback(interaction)

        mock_start_dry_run.assert_called_once()
        interaction.response.edit_message.assert_awaited()
        edit_kwargs = interaction.response.edit_message.await_args.kwargs
        embed_payload = edit_kwargs["embed"]
        if hasattr(embed_payload, "fields"):
            field_map = {
                (field.name if hasattr(field, "name") else field["name"]): (
                    field.value if hasattr(field, "value") else field["value"]
                )
                for field in embed_payload.fields
            }
        else:
            field_map = {field["name"]: field["value"] for field in embed_payload["fields"]}
        self.assertEqual(field_map["Dry Run Status"], "Dry Run Preview — Not Executed")
        self.assertEqual(field_map["Execution"], "Not dispatched")
        self.assertEqual(field_map["Publish"], "Not dispatched")
        interaction.response.send_message.assert_awaited_once()
        args, kwargs = interaction.response.send_message.await_args
        self.assertEqual(args[0], "Dry Run Preview — Not Executed")
        self.assertTrue(kwargs.get("ephemeral"))

    async def test_dry_run_preview_wrong_requester_gets_ephemeral_error(self) -> None:
        import subprocess

        from agent.coo.execution_dispatcher import get_default_dispatch_plan_store
        from agent.coo.execution_ticket import get_default_ticket_store
        from agent.coo.gateway_execution_dispatcher import create_dispatch_plan_for_gateway_session
        from agent.coo.gateway_approval import approve_gateway_session

        get_default_ticket_store().clear()
        get_default_dispatch_plan_store().clear()

        store = CEOApprovalSessionStore()
        seeded = _seed_session_in_store(store)
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            approved = approve_gateway_session(
                seeded["session_id"],
                reviewer="987654321012345678",
                requester_id="987654321012345678",
                store=store,
            )
            create_dispatch_plan_for_gateway_session(
                approved["session_id"],
                requester_id="987654321012345678",
            )

        custom_id = f"coo_approval:dry_run_preview:{approved['session_id']}"
        callback = _make_coo_approval_button_callback(custom_id, store=store)
        interaction = self._mock_interaction(user_id=1)

        with patch(
            "agent.coo.gateway_execution_runtime.start_dry_run_for_gateway_session"
        ) as mock_start_dry_run:
            await callback(interaction)

        mock_start_dry_run.assert_not_called()
        interaction.response.send_message.assert_awaited_once()
        args, kwargs = interaction.response.send_message.await_args
        self.assertEqual(args[0], _ERR_NOT_ALLOWED)
        self.assertTrue(kwargs.get("ephemeral"))


if __name__ == "__main__":
    unittest.main()
