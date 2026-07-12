"""Phase 12T tests — dispatch repository read-only attestation CLI."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_cli_repository_attestation import (
    RECOMMENDED_NEXT_PHASE,
    _FileStatSnapshot,
    _sha256_file_with_toctou,
    attest_repository2_production_root,
    format_dispatch_repository_attestation,
)
from agent.coo.dispatch_pipeline_root_trust import assert_pipeline_root_allowed
from hermes_cli.coo_dispatch import build_coo_dispatch_parser

_FORBIDDEN_OUTPUT_TOKENS = (
    "argv",
    "cwd",
    "env",
    "stdout",
    "stderr",
    "snapshot",
    "token",
    "SECRET",
    "PASSWORD",
    "phrase",
    "dependencies",
    "node pipeline.js",
    "npm install",
    "npm run",
    "npx",
)


def _repository_digest(root: Path) -> str:
    if not root.exists():
        return ""
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() or path.is_symlink():
            rel = path.relative_to(root).as_posix()
            stat = path.lstat()
            parts.append(f"{rel}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _seed_repository2_fixture(
    root: Path,
    *,
    include_optional_outputs: bool = False,
    include_git: bool = True,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pipeline.js").write_text("// pipeline entry\n", encoding="utf-8")
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "repository2-fixture",
                "version": "0.0.1",
                "scripts": {"start": "node pipeline.js"},
                "dependencies": {"secret-dep": "9.9.9"},
            }
        ),
        encoding="utf-8",
    )
    for directory in ("publishers", "prompts", "config"):
        (root / directory).mkdir()
    if include_optional_outputs:
        (root / "outputs").mkdir()
    if include_git:
        git_dir = root / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
        heads = git_dir / "refs" / "heads"
        heads.mkdir(parents=True)
        (heads / "master").write_text(
            "abcdef0123456789abcdef0123456789abcdef01\n",
            encoding="ascii",
        )


@contextmanager
def _attest_expected_root(root: Path):
    with patch(
        "agent.coo.dispatch_cli_repository_attestation.EXPECTED_REPOSITORY2_PRODUCTION_ROOT",
        str(root),
    ):
        yield


class TestRepositoryAttestationSuccess(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repository2"
        _seed_repository2_fixture(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_expected_root_structure_attestation_success(self) -> None:
        with _attest_expected_root(self.root):
            summary = attest_repository2_production_root(
                repository_root=str(self.root),
            )
        self.assertTrue(summary.repository_attested)
        self.assertTrue(summary.root_matches_expected)
        self.assertFalse(summary.root_is_symlink)
        self.assertTrue(summary.pipeline_entrypoint_present)
        self.assertTrue(summary.package_manifest_present)
        self.assertEqual(summary.required_directories_present_count, 3)
        self.assertEqual(summary.required_directories_missing, "(none)")
        self.assertEqual(summary.optional_directories_present, "(none)")
        self.assertEqual(len(summary.pipeline_sha256), 64)
        self.assertEqual(len(summary.package_sha256), 64)
        self.assertTrue(summary.git_metadata_present)
        self.assertEqual(summary.git_head_kind, "symbolic_ref")
        self.assertTrue(summary.git_head_value.startswith("master@"))
        self.assertFalse(summary.execution_allowed)
        self.assertTrue(summary.production_root_hard_deny)
        self.assertEqual(summary.recommended_next_phase, RECOMMENDED_NEXT_PHASE)

    def test_optional_directories_reported_when_present(self) -> None:
        (self.root / "outputs").mkdir()
        (self.root / "reports").mkdir()
        with _attest_expected_root(self.root):
            summary = attest_repository2_production_root(
                repository_root=str(self.root),
            )
        self.assertEqual(summary.optional_directories_present, "outputs,reports")

    def test_hashes_stable(self) -> None:
        with _attest_expected_root(self.root):
            first = attest_repository2_production_root(repository_root=str(self.root))
            second = attest_repository2_production_root(repository_root=str(self.root))
        self.assertEqual(first.pipeline_sha256, second.pipeline_sha256)
        self.assertEqual(first.package_sha256, second.package_sha256)


class TestRepositoryAttestationFailClosed(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repository2"
        _seed_repository2_fixture(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_pipeline_js_fails(self) -> None:
        (self.root / "pipeline.js").unlink()
        with _attest_expected_root(self.root):
            with self.assertRaises(ValueError) as exc:
                attest_repository2_production_root(repository_root=str(self.root))
        self.assertIn("pipeline.js", str(exc.exception))

    def test_symlink_pipeline_js_fails(self) -> None:
        real = self.root / "pipeline.real.js"
        real.write_text("// real\n", encoding="utf-8")
        (self.root / "pipeline.js").unlink()
        (self.root / "pipeline.js").symlink_to(real)
        with _attest_expected_root(self.root):
            with self.assertRaises(ValueError) as exc:
                attest_repository2_production_root(repository_root=str(self.root))
        self.assertIn("symlink", str(exc.exception))

    def test_corrupted_package_json_fails(self) -> None:
        (self.root / "package.json").write_text("{not-json", encoding="utf-8")
        with _attest_expected_root(self.root):
            with self.assertRaises(ValueError) as exc:
                attest_repository2_production_root(repository_root=str(self.root))
        self.assertIn("package.json", str(exc.exception))

    def test_missing_required_directory_fails(self) -> None:
        import shutil

        shutil.rmtree(self.root / "config")
        with _attest_expected_root(self.root):
            with self.assertRaises(ValueError) as exc:
                attest_repository2_production_root(repository_root=str(self.root))
        self.assertIn("required repository directories are missing", str(exc.exception))

    def test_root_symlink_fails(self) -> None:
        alias_root = Path(self.tmp.name) / "alias"
        alias_root.symlink_to(self.root, target_is_directory=True)
        with _attest_expected_root(self.root):
            with self.assertRaises(ValueError) as exc:
                attest_repository2_production_root(repository_root=str(alias_root))
        self.assertIn("symlink", str(exc.exception))

    def test_unexpected_root_fails(self) -> None:
        other = Path(self.tmp.name) / "other"
        _seed_repository2_fixture(other)
        with _attest_expected_root(self.root):
            with self.assertRaises(ValueError) as exc:
                attest_repository2_production_root(repository_root=str(other))
        self.assertIn("does not match expected production root", str(exc.exception))

    def test_path_traversal_fails(self) -> None:
        with _attest_expected_root(self.root):
            with self.assertRaises(ValueError) as exc:
                attest_repository2_production_root(
                    repository_root=str(self.root / ".." / self.root.name / "publishers"),
                )
        self.assertIn("path traversal", str(exc.exception))

    def test_toctou_metadata_change_fails(self) -> None:
        pipeline_path = self.root / "pipeline.js"
        before = _FileStatSnapshot(
            st_ino=pipeline_path.stat().st_ino,
            st_size=pipeline_path.stat().st_size,
            st_mtime_ns=pipeline_path.stat().st_mtime_ns,
            st_mode=pipeline_path.stat().st_mode,
        )
        after = _FileStatSnapshot(
            st_ino=before.st_ino,
            st_size=before.st_size + 1,
            st_mtime_ns=before.st_mtime_ns,
            st_mode=before.st_mode,
        )
        snapshots = iter((before, after))

        def fake_snapshot(path: Path) -> _FileStatSnapshot:
            if path == pipeline_path:
                return next(snapshots)
            stat = path.stat()
            return _FileStatSnapshot(
                st_ino=stat.st_ino,
                st_size=stat.st_size,
                st_mtime_ns=stat.st_mtime_ns,
                st_mode=stat.st_mode,
            )

        with _attest_expected_root(self.root):
            with patch(
                "agent.coo.dispatch_cli_repository_attestation._snapshot_stat",
                side_effect=fake_snapshot,
            ):
                with self.assertRaises(ValueError) as exc:
                    _sha256_file_with_toctou(pipeline_path)
        self.assertIn("metadata changed", str(exc.exception))


class TestRepositoryAttestationPolicy(unittest.TestCase):
    def test_hard_deny_maintained_for_production_root(self) -> None:
        with self.assertRaises(ValueError):
            assert_pipeline_root_allowed("/opt/data/multi-content-pipeline")

    def test_execution_allowed_false_in_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repository2"
            _seed_repository2_fixture(root)
            with _attest_expected_root(root):
                summary = attest_repository2_production_root(repository_root=str(root))
        self.assertFalse(summary.execution_allowed)
        self.assertTrue(summary.production_root_hard_deny)


class TestRepositoryAttestationSafeOutput(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repository2"
        _seed_repository2_fixture(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_package_scripts_dependencies_and_content_not_in_output(self) -> None:
        with _attest_expected_root(self.root):
            summary = attest_repository2_production_root(repository_root=str(self.root))
        output = format_dispatch_repository_attestation(summary).lower()
        self.assertNotIn("repository2-fixture", output)
        self.assertNotIn("secret-dep", output)
        self.assertNotIn("node pipeline.js", output)
        self.assertNotIn("dependencies", output)
        self.assertNotIn("// pipeline entry", output)

    def test_format_includes_required_fields(self) -> None:
        with _attest_expected_root(self.root):
            summary = attest_repository2_production_root(repository_root=str(self.root))
        output = format_dispatch_repository_attestation(summary)
        self.assertIn("Repository Attestation", output)
        self.assertIn("repository_attested: true", output)
        self.assertIn("execution_allowed: false", output)
        self.assertIn("production_root_hard_deny: true", output)
        self.assertIn("recommended_next_phase:", output)

    def test_safe_output_has_no_forbidden_tokens(self) -> None:
        with _attest_expected_root(self.root):
            summary = attest_repository2_production_root(repository_root=str(self.root))
        output = format_dispatch_repository_attestation(summary).lower()
        for token in _FORBIDDEN_OUTPUT_TOKENS:
            self.assertNotIn(token.lower(), output)


class TestRepositoryAttestationCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repository2"
        _seed_repository2_fixture(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cli_attest_success_exit_zero(self) -> None:
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            ["repository", "attest", "--repository-root", str(self.root)],
        )
        buf = io.StringIO()
        with (
            _attest_expected_root(self.root),
            patch("sys.stdout", buf),
        ):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 0)
        self.assertIn("repository_attested: true", buf.getvalue())

    def test_cli_attest_failure_exit_one(self) -> None:
        (self.root / "pipeline.js").unlink()
        parser = build_coo_dispatch_parser()
        args = parser.parse_args(
            ["repository", "attest", "--repository-root", str(self.root)],
        )
        with (
            _attest_expected_root(self.root),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", io.StringIO()),
        ):
            exit_code = args.handler(args)
        self.assertEqual(exit_code, 1)

    def test_read_only_no_subprocess_or_writes(self) -> None:
        before = _repository_digest(self.root)
        with (
            _attest_expected_root(self.root),
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
            patch("shutil.which", side_effect=AssertionError("no shell lookup")),
        ):
            attest_repository2_production_root(repository_root=str(self.root))
            format_dispatch_repository_attestation(
                attest_repository2_production_root(repository_root=str(self.root)),
            )
        self.assertEqual(_repository_digest(self.root), before)


class TestRepositoryAttestationProductionSmoke(unittest.TestCase):
    _PRODUCTION_ROOT = Path("/opt/data/multi-content-pipeline")

    def test_read_only_smoke_when_production_root_exists(self) -> None:
        if not self._PRODUCTION_ROOT.is_dir():
            self.skipTest("production Repository2 root is not available")
        before = _repository_digest(self._PRODUCTION_ROOT)
        with (
            patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")),
            patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")),
        ):
            summary = attest_repository2_production_root(
                repository_root=str(self._PRODUCTION_ROOT),
            )
        self.assertTrue(summary.repository_attested)
        self.assertTrue(summary.root_matches_expected)
        self.assertFalse(summary.execution_allowed)
        self.assertEqual(_repository_digest(self._PRODUCTION_ROOT), before)

        with self.assertRaises(ValueError):
            assert_pipeline_root_allowed(str(self._PRODUCTION_ROOT.resolve()))


class TestProductionReadinessRegression(unittest.TestCase):
    def test_readiness_still_reports_execution_disabled(self) -> None:
        from agent.coo.dispatch_cli_production_readiness import (
            RECOMMENDED_NEXT_PHASE_READY,
            evaluate_dispatch_production_readiness,
        )

        summary = evaluate_dispatch_production_readiness()
        self.assertEqual(summary.recommended_next_phase, RECOMMENDED_NEXT_PHASE_READY)
        self.assertEqual(summary.repository2_policy.execution_disabled, "enabled")
        self.assertEqual(summary.repository2_policy.production_root_hard_deny, "enabled")
