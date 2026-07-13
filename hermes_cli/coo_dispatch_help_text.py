"""Shared COO dispatch CLI help phrases — Phase 13Q."""

from __future__ import annotations

HELP_READ_ONLY = "Read-only command."
HELP_OPERATOR_CONFIRMATION = "Operator confirmation required."
HELP_ISOLATED_MOCK = "Isolated mock scope only."
HELP_PRODUCTION_BLOCKED = "Production execution remains disabled."
HELP_REPOSITORY_HARD_DENY = "Repository2 production root remains hard-denied."

SAFETY_FOOTER = (
    "Safety: production execution remains disabled; "
    "Repository2 production root remains hard-denied; "
    "isolated mock scope only for gateway pilot."
)

DOCS_OPERATOR_INDEX = "docs/operator/README.md"


def read_only(help_text: str) -> str:
    return f"{help_text} {HELP_READ_ONLY}"


def write_gated(help_text: str) -> str:
    return (
        f"{help_text} {HELP_OPERATOR_CONFIRMATION} "
        f"{HELP_PRODUCTION_BLOCKED}"
    )


def mock_pilot(help_text: str) -> str:
    return (
        f"{help_text} {HELP_ISOLATED_MOCK} {HELP_PRODUCTION_BLOCKED}"
    )


def repository_read_only(help_text: str) -> str:
    return (
        f"{help_text} {HELP_READ_ONLY} {HELP_REPOSITORY_HARD_DENY} "
        f"{HELP_PRODUCTION_BLOCKED}"
    )
