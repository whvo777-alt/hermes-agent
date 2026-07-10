"""Phase 11A tests — pipeline root trust and attestation helpers."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.dispatch_pipeline_root_trust import (
    assert_pipeline_root_matches_attestation,
    assert_pipeline_root_trusted,
    validate_stored_attested_pipeline_root,
)


class TestDispatchPipelineRootTrust(unittest.TestCase):
    def test_isolated_root_trusted_and_stored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "isolated"
            root.mkdir()
            resolved = assert_pipeline_root_trusted(str(root))
            self.assertEqual(resolved, str(root.resolve()))
            self.assertEqual(validate_stored_attested_pipeline_root(resolved), resolved)

    def test_empty_root_rejected(self) -> None:
        with self.assertRaises(ValueError):
            assert_pipeline_root_trusted("")

    def test_relative_root_rejected_for_cli(self) -> None:
        with self.assertRaises(ValueError):
            assert_pipeline_root_trusted("relative/path")

    def test_relative_root_rejected_on_read(self) -> None:
        with self.assertRaises(ValueError):
            validate_stored_attested_pipeline_root("relative/path")

    def test_production_root_rejected(self) -> None:
        with self.assertRaises(ValueError):
            assert_pipeline_root_trusted("/opt/data/multi-content-pipeline")

    def test_production_symlink_escape_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            denied_root = os.path.join(tmp, "denied-root")
            internal_dir = os.path.join(denied_root, "outputs")
            os.makedirs(internal_dir)
            link_path = os.path.join(tmp, "escape-link")
            os.symlink(internal_dir, link_path)
            with patch(
                "agent.coo.dispatch_pipeline_root_trust.PRODUCTION_ROOT_HARD_DENY",
                (denied_root,),
            ):
                with self.assertRaises(ValueError):
                    assert_pipeline_root_trusted(link_path)

    def test_attestation_mismatch_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "a"
            root_b = Path(tmp) / "b"
            root_a.mkdir()
            root_b.mkdir()
            attested = assert_pipeline_root_trusted(str(root_a))
            with self.assertRaises(ValueError):
                assert_pipeline_root_matches_attestation(
                    cli_pipeline_root=str(root_b),
                    attested_pipeline_root=attested,
                )
