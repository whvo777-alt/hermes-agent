"""Tests for gateway.run._check_and_alert_dead_credentials -- the piece the
housekeeping loop ticks every 5 minutes to alert on Discord when a
credential is permanently STATUS_DEAD. Split out of the housekeeping loop
itself (which has no test harness) so this new logic is independently
testable without spinning up the whole background thread.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import Platform


def _make_adapters(send_mock=None):
    adapter = MagicMock()
    adapter.send = send_mock or AsyncMock()
    return {Platform.DISCORD: adapter}, adapter


def test_no_dead_credentials_sends_nothing():
    from gateway.run import _check_and_alert_dead_credentials

    adapters, adapter = _make_adapters()
    with patch("agent.credential_pool.get_dead_credentials", return_value=[]):
        _check_and_alert_dead_credentials(adapters, None, {}, resend_seconds=3600)
    adapter.send.assert_not_called()


def test_dead_credential_triggers_send_to_home_channel():
    from gateway.run import _check_and_alert_dead_credentials

    adapters, adapter = _make_adapters()
    dead = [{
        "provider": "openai-codex", "label": "openai-codex-oauth-1", "entry_id": "abc123",
        "last_error_reason": "token_invalidated", "last_error_message": None, "last_status_at": 0.0,
    }]
    with patch("agent.credential_pool.get_dead_credentials", return_value=dead), \
         patch("cron.scheduler._get_home_target_chat_id", return_value="123456"):
        # loop=None: safe_schedule_threadsafe's own documented no-op-and-close
        # behavior applies (no real event loop needed in this unit test).
        alert_sent_at = {}
        _check_and_alert_dead_credentials(adapters, None, alert_sent_at, resend_seconds=3600)
    assert adapter.send.called
    assert "openai-codex:abc123" in alert_sent_at


def test_no_discord_adapter_is_a_quiet_noop():
    from gateway.run import _check_and_alert_dead_credentials

    dead = [{"provider": "openai-codex", "label": "x", "entry_id": "1",
             "last_error_reason": "r", "last_error_message": None, "last_status_at": 0.0}]
    with patch("agent.credential_pool.get_dead_credentials", return_value=dead):
        # No Platform.DISCORD key in adapters at all.
        _check_and_alert_dead_credentials({}, None, {}, resend_seconds=3600)  # must not raise


def test_no_home_channel_configured_is_a_quiet_noop():
    from gateway.run import _check_and_alert_dead_credentials

    adapters, adapter = _make_adapters()
    dead = [{"provider": "openai-codex", "label": "x", "entry_id": "1",
             "last_error_reason": "r", "last_error_message": None, "last_status_at": 0.0}]
    with patch("agent.credential_pool.get_dead_credentials", return_value=dead), \
         patch("cron.scheduler._get_home_target_chat_id", return_value=""):
        _check_and_alert_dead_credentials(adapters, None, {}, resend_seconds=3600)
    adapter.send.assert_not_called()


def test_same_dead_credential_is_not_repaged_within_resend_window():
    from gateway.run import _check_and_alert_dead_credentials

    adapters, adapter = _make_adapters()
    dead = [{"provider": "openai-codex", "label": "x", "entry_id": "1",
             "last_error_reason": "r", "last_error_message": None, "last_status_at": 0.0}]
    with patch("agent.credential_pool.get_dead_credentials", return_value=dead), \
         patch("cron.scheduler._get_home_target_chat_id", return_value="123456"):
        alert_sent_at: dict = {}
        _check_and_alert_dead_credentials(adapters, None, alert_sent_at, resend_seconds=3600)
        assert adapter.send.call_count == 1
        # Second tick, well within the resend window -- must not send again.
        _check_and_alert_dead_credentials(adapters, None, alert_sent_at, resend_seconds=3600)
        assert adapter.send.call_count == 1


def test_dead_credential_is_repaged_after_resend_window_elapses():
    from gateway.run import _check_and_alert_dead_credentials

    adapters, adapter = _make_adapters()
    dead = [{"provider": "openai-codex", "label": "x", "entry_id": "1",
             "last_error_reason": "r", "last_error_message": None, "last_status_at": 0.0}]
    with patch("agent.credential_pool.get_dead_credentials", return_value=dead), \
         patch("cron.scheduler._get_home_target_chat_id", return_value="123456"):
        alert_sent_at = {"openai-codex:1": 0.0}  # "already alerted" long ago (epoch 0)
        _check_and_alert_dead_credentials(adapters, None, alert_sent_at, resend_seconds=1)
    assert adapter.send.call_count == 1
    assert alert_sent_at["openai-codex:1"] > 0.0
