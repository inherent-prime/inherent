"""Keep the CLI reference aligned with the commands registered by Typer."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_reference_names_every_registered_cli_command() -> None:
    reference = (REPO_ROOT / "docs/reference/cli.md").read_text()
    commands = (
        "inherent up",
        "inherent down",
        "inherent status",
        "inherent logs",
        "inherent doctor",
        "inherent docs list",
        "inherent docs show",
        "inherent docs upload",
        "inherent docs delete",
        "inherent docs refresh",
        "inherent docs lineage",
        "inherent chunks",
        "inherent search",
        "inherent whoami",
        "inherent workspaces list",
        "inherent keys list",
        "inherent keys create",
        "inherent keys revoke",
        "inherent connect",
    )
    assert not [command for command in commands if command not in reference]
