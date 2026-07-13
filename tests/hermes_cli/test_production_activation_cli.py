"""Phase 14C tests — production activation proposal CLI and store."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_production_activation import (
    ProductionActivationCliError,
    build_production_activation_proposal,
    format_activation_proposal_output,
    propose_production_activation,
    run_production_activation_propose,
)
from agent.coo.production_activation_state import (
    ACTIVATION_PLATFORM_CLI,
    ACTIVATION_SCOPE_ONE_SHOT,
    ACTIVATION_SCOPE_TICKET_SCOPED,
    ACTIVATION_STATE_PROPOSED,
    ProductionActivationStateError,
)
from agent.coo.production_activation_store import (
    ProductionActivationStoreError,
    append_activation_proposal,
    find_open_proposed_activation_id,
    load_activation_request,
)
from hermes_cli.coo_dispatch import build_coo_dispatch_parser

_TESTED_SHA = "ca269dab24ffceb43ddfeb44c76a5120f987dc46"
_ROLLBACK_SHA = "18a03673739262534847af0296458239511bb7e6"
_ATTESTATION_HASH = "a" * 64

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
    "rollback_commit",
    "repository_attestation_hash",
)


def _hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    (home / "coo" / "production-activation").mkdir(parents=True)
    return home


def _activation_store_dir(hermes_home: Path) -> Path:
    return hermes_home / "coo" / "production-activation"


def _init_git_repo(repo_root: Path, commit_sha: str = _TESTED_SHA) -> None:
    git_dir = repo_root / ".git"
    refs_dir = git_dir / "refs" / "heads"
    refs_dir.mkdir(parents=True)
    (refs_dir / "main").write_text(f"{commit_sha}\n", encoding="utf-8")
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


def _proposal_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tested_commit_sha": _TESTED_SHA,
        "release_tag": "v1.0.0-rc.1",
        "repository_attestation_hash": _ATTESTATION_HASH,
        "requested_by": "operator-a",
        "rollback_commit": _ROLLBACK_SHA,
        "scope_type": ACTIVATION_SCOPE_ONE_SHOT,
        "platform": ACTIVATION_PLATFORM_CLI,
    }
    base.update(overrides)
    return base


def _hermes_digest(root: Path) -> str:
    if not root.exists():
        return ""
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            parts.append(f"{rel}:{digest}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


class TestProductionActivationProposalCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.hermes_home = _hermes_home(self.tmp_path)
        self.repo_root = self.tmp_path / "repo"
        self.repo_root.mkdir()
        _init_git_repo(self.repo_root)
        self.store_dir = _activation_store_dir(self.hermes_home)
        self.env_patch = patch.dict(
            "os.environ",
            {"HERMES_HOME": str(self.hermes_home)},
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self._tmp.cleanup()

    def test_propose_creates_append_only_artifact(self) -> None:
        request = propose_production_activation(
            **_proposal_kwargs(),
            repo_root=self.repo_root,
            store_dir=self.store_dir,
        )
        self.assertEqual(request.state, ACTIVATION_STATE_PROPOSED)
        path = self.store_dir / f"{request.activation_request_id}.json"
        self.assertTrue(path.is_file())
        loaded = load_activation_request(
            request.activation_request_id,
            store_dir=self.store_dir,
        )
        self.assertEqual(loaded.tested_commit_sha, _TESTED_SHA)
        self.assertEqual(len(loaded.state_history), 1)
        self.assertEqual(len(loaded.approval_history), 0)

    def test_cli_parser_propose_subcommand(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            [
                "production",
                "activation",
                "propose",
                "--tested-commit-sha",
                _TESTED_SHA,
                "--release-tag",
                "v1.0.0-rc.1",
                "--repository-attestation-hash",
                _ATTESTATION_HASH,
                "--requested-by",
                "operator-a",
                "--rollback-commit",
                _ROLLBACK_SHA,
                "--activation-scope",
                "one_shot",
            ]
        )
        self.assertEqual(args.coo_dispatch_production_activation_command, "propose")
        self.assertEqual(args.scope_type, ACTIVATION_SCOPE_ONE_SHOT)

    def test_head_sha_mismatch_fail_closed(self) -> None:
        with self.assertRaises(ProductionActivationCliError):
            build_production_activation_proposal(
                **_proposal_kwargs(tested_commit_sha="deadbeef"),
                repo_root=self.repo_root,
            )

    def test_invalid_tag_fail_closed(self) -> None:
        with self.assertRaises(ProductionActivationStateError):
            build_production_activation_proposal(
                **_proposal_kwargs(release_tag="not-a-tag"),
                repo_root=self.repo_root,
            )

    def test_missing_rollback_fail_closed(self) -> None:
        with self.assertRaises(ProductionActivationStateError):
            build_production_activation_proposal(
                **_proposal_kwargs(rollback_commit=""),
                repo_root=self.repo_root,
            )

    def test_ticket_scoped_requires_ticket_id(self) -> None:
        with self.assertRaises(ProductionActivationStateError):
            build_production_activation_proposal(
                **_proposal_kwargs(
                    scope_type=ACTIVATION_SCOPE_TICKET_SCOPED,
                    ticket_id="",
                ),
                repo_root=self.repo_root,
            )

    def test_duplicate_proposal_id_fail_closed(self) -> None:
        request = build_production_activation_proposal(
            **_proposal_kwargs(),
            repo_root=self.repo_root,
            activation_request_id=str(uuid.uuid4()),
        )
        append_activation_proposal(request, store_dir=self.store_dir)
        with self.assertRaises(ProductionActivationStoreError):
            append_activation_proposal(request, store_dir=self.store_dir)

    def test_open_proposal_uniqueness_fail_closed(self) -> None:
        propose_production_activation(
            **_proposal_kwargs(),
            repo_root=self.repo_root,
            store_dir=self.store_dir,
        )
        with self.assertRaises(ProductionActivationCliError):
            propose_production_activation(
                **_proposal_kwargs(requested_by="operator-b"),
                repo_root=self.repo_root,
                store_dir=self.store_dir,
            )
        self.assertIsNotNone(find_open_proposed_activation_id(store_dir=self.store_dir))

    def test_safe_output_fields(self) -> None:
        request = build_production_activation_proposal(
            **_proposal_kwargs(),
            repo_root=self.repo_root,
        )
        output = format_activation_proposal_output(request)
        self.assertIn("activation_request_id:", output)
        self.assertIn("state: proposed", output)
        self.assertIn("tested_commit_sha: ca269dab24ff", output)
        self.assertIn("release_tag: v1.0.0-rc.1", output)
        self.assertIn("scope_type: one_shot", output)
        self.assertIn("expires_at:", output)
        self.assertIn("production_execution_allowed: false", output)
        lowered = output.lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), lowered)

    def test_run_cli_success(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_production_activation.resolve_git_head_commit",
            return_value=_TESTED_SHA,
        ):
            output, exit_code = run_production_activation_propose(
                **_proposal_kwargs(),
                store_dir=self.store_dir,
            )
        self.assertEqual(exit_code, 0)
        self.assertIn("Production Activation Proposal", output)

    def test_no_subprocess(self) -> None:
        with patch.object(
            subprocess,
            "run",
            side_effect=AssertionError("no subprocess"),
        ), patch.object(
            subprocess,
            "Popen",
            side_effect=AssertionError("no subprocess"),
        ), patch(
            "agent.coo.dispatch_cli_production_activation.resolve_git_head_commit",
            return_value=_TESTED_SHA,
        ):
            run_production_activation_propose(
                **_proposal_kwargs(),
                store_dir=self.store_dir,
            )

    def test_store_record_has_no_forbidden_keys(self) -> None:
        with patch(
            "agent.coo.dispatch_cli_production_activation.resolve_git_head_commit",
            return_value=_TESTED_SHA,
        ):
            request = propose_production_activation(
                **_proposal_kwargs(),
                store_dir=self.store_dir,
            )
        payload = json.loads(
            (self.store_dir / f"{request.activation_request_id}.json").read_text(
                encoding="utf-8"
            )
        )
        lowered_keys = " ".join(payload.keys()).lower()
        for token in ("confirmation_phrase", "unlock_token", "pipeline_root", "argv"):
            self.assertNotIn(token, lowered_keys)

    def test_digest_only_changes_activation_store(self) -> None:
        digest_before = _hermes_digest(self.hermes_home)
        with patch(
            "agent.coo.dispatch_cli_production_activation.resolve_git_head_commit",
            return_value=_TESTED_SHA,
        ):
            run_production_activation_propose(
                **_proposal_kwargs(),
                store_dir=self.store_dir,
            )
        digest_after = _hermes_digest(self.hermes_home)
        self.assertNotEqual(digest_before, digest_after)
        changed = {
            path.relative_to(self.hermes_home).as_posix()
            for path in self.hermes_home.rglob("*")
            if path.is_file()
        }
        for rel in changed:
            self.assertTrue(rel.startswith("coo/production-activation/"))


class TestProductionActivationStoreErrors(unittest.TestCase):
    def test_overwrite_raises_store_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_git_repo(root)
            hermes_home = _hermes_home(root)
            store_dir = _activation_store_dir(hermes_home)
            with patch.dict("os.environ", {"HERMES_HOME": str(hermes_home)}):
                request = build_production_activation_proposal(
                    **_proposal_kwargs(),
                    repo_root=root,
                    activation_request_id=str(uuid.uuid4()),
                )
                append_activation_proposal(request, store_dir=store_dir)
                with self.assertRaises(ProductionActivationStoreError):
                    append_activation_proposal(request, store_dir=store_dir)


if __name__ == "__main__":
    unittest.main()
