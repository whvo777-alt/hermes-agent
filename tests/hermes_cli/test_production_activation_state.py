"""Phase 14B tests — production activation state model."""

from __future__ import annotations

import subprocess
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agent.coo.production_activation_state import (
    ACTIVATION_PLATFORM_CLI,
    ACTIVATION_SCOPE_ONE_SHOT,
    ACTIVATION_SCOPE_TICKET_SCOPED,
    ACTIVATION_STATE_ACTIVE,
    ACTIVATION_STATE_APPROVED,
    ACTIVATION_STATE_ARMED,
    ACTIVATION_STATE_DISABLED,
    ACTIVATION_STATE_PROPOSED,
    ACTIVATION_STATE_REVOKED,
    ACTIVATION_STATE_SUSPENDED,
    ActivationApprovalRecord,
    ActivationRequest,
    ActivationScope,
    ActivationStateTransition,
    ProductionActivationStateError,
    ROLE_OPERATOR,
    ROLE_PRODUCTION_EXECUTOR,
    ROLE_RELEASE_APPROVER,
    ROLE_SECURITY_REVIEWER,
    format_activation_request,
    validate_activation_request,
    validate_activation_transition,
)

_FORBIDDEN_OUTPUT_TOKENS = (
    "pipeline_root",
    "confirmation_phrase",
    "unlock_token",
    "/opt/data/",
    "pipeline.js",
    "argv",
    "cwd",
    "stdout",
    "stderr",
    "secret",
)

_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def _iso(offset_minutes: int = 0) -> str:
    return (_NOW + timedelta(minutes=offset_minutes)).isoformat()


def _transition(
    from_state: str,
    to_state: str,
    *,
    offset: int = 0,
    actor: str = "operator-a",
    role: str = ROLE_OPERATOR,
    reason: str = "test_transition",
) -> ActivationStateTransition:
    return ActivationStateTransition(
        from_state=from_state,
        to_state=to_state,
        actor=actor,
        role=role,
        timestamp=_iso(offset),
        reason_code=reason,
    )


def _scope(**overrides: object) -> ActivationScope:
    base = {
        "scope_type": ACTIVATION_SCOPE_ONE_SHOT,
        "platform": ACTIVATION_PLATFORM_CLI,
        "publish_allowed": False,
        "ticket_id": "",
    }
    base.update(overrides)
    return ActivationScope(**base)


def _disabled_request() -> ActivationRequest:
    return ActivationRequest(
        activation_request_id=str(uuid.uuid4()),
        tested_commit_sha="",
        release_tag="",
        repository_attestation_hash="",
        requested_by="operator-a",
        approved_by=(),
        security_reviewed_by="",
        activation_scope=_scope(),
        rollback_commit="",
        state=ACTIVATION_STATE_DISABLED,
        created_at=_iso(),
        updated_at=_iso(),
        state_history=(),
        approval_history=(),
        expires_at="",
        armed_expires_at="",
        active_expires_at="",
    )


def _proposed_request() -> ActivationRequest:
    return ActivationRequest(
        activation_request_id=str(uuid.uuid4()),
        tested_commit_sha="18a03673739262534847af0296458239511bb7e6",
        release_tag="v1.0.0-rc.1",
        repository_attestation_hash="a" * 64,
        requested_by="operator-a",
        approved_by=(),
        security_reviewed_by="",
        activation_scope=_scope(),
        rollback_commit="01a1068332602e9248b1965783156a76d83d623a",
        state=ACTIVATION_STATE_PROPOSED,
        created_at=_iso(),
        updated_at=_iso(1),
        state_history=(
            _transition(ACTIVATION_STATE_DISABLED, ACTIVATION_STATE_PROPOSED),
        ),
        approval_history=(),
        expires_at="",
        armed_expires_at="",
        active_expires_at="",
    )


def _approved_request() -> ActivationRequest:
    return ActivationRequest(
        activation_request_id=str(uuid.uuid4()),
        tested_commit_sha="18a03673739262534847af0296458239511bb7e6",
        release_tag="v1.0.0-rc.1",
        repository_attestation_hash="b" * 64,
        requested_by="operator-a",
        approved_by=("approver-b", "approver-c"),
        security_reviewed_by="security-d",
        activation_scope=_scope(),
        rollback_commit="01a1068332602e9248b1965783156a76d83d623a",
        state=ACTIVATION_STATE_APPROVED,
        created_at=_iso(),
        updated_at=_iso(2),
        state_history=(
            _transition(ACTIVATION_STATE_DISABLED, ACTIVATION_STATE_PROPOSED, offset=0),
            _transition(
                ACTIVATION_STATE_PROPOSED,
                ACTIVATION_STATE_APPROVED,
                offset=1,
                actor="approver-b",
                role=ROLE_RELEASE_APPROVER,
            ),
        ),
        approval_history=(
            ActivationApprovalRecord(
                approver_id="approver-b",
                role=ROLE_RELEASE_APPROVER,
                timestamp=_iso(1),
            ),
            ActivationApprovalRecord(
                approver_id="security-d",
                role=ROLE_SECURITY_REVIEWER,
                timestamp=_iso(2),
            ),
        ),
        expires_at=_iso(60),
        armed_expires_at="",
        active_expires_at="",
    )


class TestActivationTransitions(unittest.TestCase):
    def test_allowed_transitions(self) -> None:
        validate_activation_transition(ACTIVATION_STATE_DISABLED, ACTIVATION_STATE_PROPOSED)
        validate_activation_transition(ACTIVATION_STATE_PROPOSED, ACTIVATION_STATE_APPROVED)
        validate_activation_transition(ACTIVATION_STATE_APPROVED, ACTIVATION_STATE_ARMED)
        validate_activation_transition(ACTIVATION_STATE_APPROVED, ACTIVATION_STATE_REVOKED)
        validate_activation_transition(ACTIVATION_STATE_ARMED, ACTIVATION_STATE_ACTIVE)
        validate_activation_transition(ACTIVATION_STATE_ARMED, ACTIVATION_STATE_REVOKED)
        validate_activation_transition(ACTIVATION_STATE_ARMED, ACTIVATION_STATE_SUSPENDED)
        validate_activation_transition(ACTIVATION_STATE_SUSPENDED, ACTIVATION_STATE_REVOKED)
        validate_activation_transition(ACTIVATION_STATE_ACTIVE, ACTIVATION_STATE_SUSPENDED)

    def test_unknown_transition_rejected(self) -> None:
        with self.assertRaises(ProductionActivationStateError):
            validate_activation_transition(ACTIVATION_STATE_PROPOSED, ACTIVATION_STATE_ARMED)
        with self.assertRaises(ProductionActivationStateError):
            validate_activation_transition(ACTIVATION_STATE_REVOKED, ACTIVATION_STATE_ACTIVE)
        with self.assertRaises(ProductionActivationStateError):
            validate_activation_transition(ACTIVATION_STATE_SUSPENDED, ACTIVATION_STATE_ACTIVE)

    def test_invalid_state_names_rejected(self) -> None:
        with self.assertRaises(ProductionActivationStateError):
            validate_activation_transition("bogus", ACTIVATION_STATE_PROPOSED)


class TestActivationValidation(unittest.TestCase):
    def test_disabled_request_validates(self) -> None:
        validated = validate_activation_request(_disabled_request())
        self.assertEqual(validated.state, ACTIVATION_STATE_DISABLED)

    def test_proposed_request_validates(self) -> None:
        validated = validate_activation_request(_proposed_request())
        self.assertEqual(validated.state, ACTIVATION_STATE_PROPOSED)

    def test_approved_request_validates(self) -> None:
        validated = validate_activation_request(_approved_request())
        self.assertEqual(validated.state, ACTIVATION_STATE_APPROVED)
        self.assertEqual(len(validated.approved_by), 2)

    def test_empty_rollback_commit_rejected(self) -> None:
        request = _proposed_request()
        bad = ActivationRequest(
            **{
                **request.__dict__,
                "rollback_commit": "",
            }
        )
        with self.assertRaises(ProductionActivationStateError):
            validate_activation_request(bad)

    def test_missing_release_tag_rejected(self) -> None:
        request = _proposed_request()
        bad = ActivationRequest(**{**request.__dict__, "release_tag": ""})
        with self.assertRaises(ProductionActivationStateError):
            validate_activation_request(bad)

    def test_missing_tested_sha_rejected(self) -> None:
        request = _proposed_request()
        bad = ActivationRequest(**{**request.__dict__, "tested_commit_sha": ""})
        with self.assertRaises(ProductionActivationStateError):
            validate_activation_request(bad)

    def test_insufficient_approvers_rejected(self) -> None:
        request = _approved_request()
        bad = ActivationRequest(**{**request.__dict__, "approved_by": ("approver-b",)})
        with self.assertRaises(ProductionActivationStateError):
            validate_activation_request(bad)

    def test_requester_in_approved_by_rejected(self) -> None:
        request = _approved_request()
        bad = ActivationRequest(
            **{**request.__dict__, "approved_by": ("operator-a", "approver-c")}
        )
        with self.assertRaises(ProductionActivationStateError):
            validate_activation_request(bad)

    def test_publish_allowed_must_remain_false(self) -> None:
        request = _proposed_request()
        bad = ActivationRequest(
            **{
                **request.__dict__,
                "activation_scope": _scope(publish_allowed=True),
            }
        )
        with self.assertRaises(ProductionActivationStateError):
            validate_activation_request(bad)

    def test_ticket_scoped_requires_ticket_id(self) -> None:
        request = _proposed_request()
        bad = ActivationRequest(
            **{
                **request.__dict__,
                "activation_scope": _scope(
                    scope_type=ACTIVATION_SCOPE_TICKET_SCOPED,
                    ticket_id="",
                ),
            }
        )
        with self.assertRaises(ProductionActivationStateError):
            validate_activation_request(bad)


class TestActivationFailClosed(unittest.TestCase):
    def test_broken_state_history_chain_rejected(self) -> None:
        request = _proposed_request()
        bad = ActivationRequest(
            **{
                **request.__dict__,
                "state_history": (
                    _transition(ACTIVATION_STATE_DISABLED, ACTIVATION_STATE_APPROVED),
                ),
            }
        )
        with self.assertRaises(ProductionActivationStateError):
            validate_activation_request(bad)

    def test_non_monotonic_history_rejected(self) -> None:
        request = _approved_request()
        bad = ActivationRequest(
            **{
                **request.__dict__,
                "state_history": (
                    _transition(
                        ACTIVATION_STATE_DISABLED,
                        ACTIVATION_STATE_PROPOSED,
                        offset=5,
                    ),
                    _transition(
                        ACTIVATION_STATE_PROPOSED,
                        ACTIVATION_STATE_APPROVED,
                        offset=1,
                    ),
                ),
            }
        )
        with self.assertRaises(ProductionActivationStateError):
            validate_activation_request(bad)

    def test_ttl_reversal_rejected(self) -> None:
        request = _approved_request()
        bad = ActivationRequest(
            **{
                **request.__dict__,
                "state": ACTIVATION_STATE_ARMED,
                "executor_id": "executor-e",
                "phrase_verified": True,
                "armed_at": _iso(3),
                "armed_expires_at": _iso(90),
                "active_expires_at": _iso(30),
                "state_history": (
                    *request.state_history,
                    _transition(
                        ACTIVATION_STATE_APPROVED,
                        ACTIVATION_STATE_ARMED,
                        offset=3,
                        actor="executor-e",
                        role=ROLE_PRODUCTION_EXECUTOR,
                    ),
                ),
            }
        )
        with self.assertRaises(ProductionActivationStateError):
            validate_activation_request(bad)


class TestActivationSafeOutput(unittest.TestCase):
    def test_format_activation_request_safe(self) -> None:
        output = format_activation_request(_approved_request())
        self.assertIn("activation_request_id:", output)
        self.assertIn("production_execution_allowed: false", output)
        self.assertIn("publish_allowed: false", output)
        lowered = output.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_format_does_not_emit_approved_by_ids(self) -> None:
        output = format_activation_request(_approved_request())
        self.assertIn("approved_by_count: 2", output)
        self.assertNotIn("approver-b", output)


class TestActivationReadOnly(unittest.TestCase):
    def test_no_subprocess(self) -> None:
        with patch.object(
            subprocess,
            "run",
            side_effect=AssertionError("no subprocess"),
        ), patch.object(
            subprocess,
            "Popen",
            side_effect=AssertionError("no subprocess"),
        ):
            validate_activation_request(_disabled_request())
            format_activation_request(_proposed_request())

    def test_module_has_no_store_imports(self) -> None:
        import ast
        from pathlib import Path

        import agent.coo.production_activation_state as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("subprocess", imported)
        self.assertNotIn("hermes_constants", imported)


class TestActivationArmedActiveStates(unittest.TestCase):
    def _armed_request(self) -> ActivationRequest:
        approved = _approved_request()
        return ActivationRequest(
            **{
                **approved.__dict__,
                "state": ACTIVATION_STATE_ARMED,
                "updated_at": _iso(3),
                "armed_at": _iso(3),
                "armed_expires_at": _iso(18),
                "executor_id": "executor-e",
                "phrase_verified": True,
                "state_history": (
                    *approved.state_history,
                    _transition(
                        ACTIVATION_STATE_APPROVED,
                        ACTIVATION_STATE_ARMED,
                        offset=3,
                        actor="executor-e",
                        role=ROLE_PRODUCTION_EXECUTOR,
                    ),
                ),
            }
        )

    def test_armed_request_validates(self) -> None:
        validated = validate_activation_request(self._armed_request())
        self.assertEqual(validated.state, ACTIVATION_STATE_ARMED)

    def test_active_request_requires_active_ttl(self) -> None:
        armed = self._armed_request()
        active = ActivationRequest(
            **{
                **armed.__dict__,
                "state": ACTIVATION_STATE_ACTIVE,
                "updated_at": _iso(4),
                "active_expires_at": _iso(64),
                "state_history": (
                    *armed.state_history,
                    _transition(
                        ACTIVATION_STATE_ARMED,
                        ACTIVATION_STATE_ACTIVE,
                        offset=4,
                        actor="executor-e",
                    ),
                ),
            }
        )
        validated = validate_activation_request(active)
        self.assertEqual(validated.state, ACTIVATION_STATE_ACTIVE)


if __name__ == "__main__":
    unittest.main()
