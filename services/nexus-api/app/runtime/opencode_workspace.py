"""Workspace-owned OpenCode configuration discovery.

Astra never generates OpenCode configuration and never sets ``OPENCODE_CONFIG``.
Each employee workspace (or the host's user-level config) owns its providers,
models, MCP servers and permissions. This module only answers whether OpenCode
would discover a configuration from a directory, so a missing configuration
fails loudly instead of silently starting an unusable Server.
"""

from __future__ import annotations

from pathlib import Path

CONFIG_FILENAMES = ("opencode.json", "opencode.jsonc")


def global_config_path() -> Path | None:
    """The user-level OpenCode config Astra falls back to, if present."""

    home = Path.home()
    for candidate in (
        home / ".config" / "opencode" / "opencode.json",
        home / ".config" / "opencode" / "opencode.jsonc",
    ):
        if candidate.is_file():
            return candidate
    return None


def discover_workspace_config(workspace: Path) -> Path | None:
    """Locate the config OpenCode discovers from ``workspace``.

    OpenCode merges ``opencode.json``(c) found from the working directory up to
    the git root, plus the user-level config. The walk stops at the git root so
    a task git worktree never inherits an unrelated parent project.
    """

    current = Path(workspace).resolve()
    while True:
        for name in CONFIG_FILENAMES:
            for candidate in (current / name, current / ".opencode" / name):
                if candidate.is_file():
                    return candidate
        if (current / ".git").exists():
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return global_config_path()