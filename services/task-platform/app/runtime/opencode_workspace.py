"""Workspace-owned OpenCode configuration discovery.

Workspace-owned configuration keeps precedence. When a workspace has none,
Nexus may fall back to an explicit deployment-owned ``NEXUS_OPENCODE_CONFIG``
(or ``OPENCODE_CONFIG``) before the host user-level config. This module only
answers whether OpenCode would discover a configuration, so a missing
configuration fails loudly instead of silently starting an unusable Server.
"""

from __future__ import annotations

import os
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


def workspace_owned_config(workspace: Path) -> Path | None:
    """Return only configs discovered inside the workspace/git root."""

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
    return None


def explicit_deployment_config() -> Path | None:
    """Deployment-owned config used only when the workspace has none."""

    for name in ("NEXUS_OPENCODE_CONFIG", "OPENCODE_CONFIG"):
        raw = (os.environ.get(name) or "").strip()
        if raw:
            candidate = Path(raw).expanduser()
            if candidate.is_file():
                return candidate
    return None


def discover_workspace_config(workspace: Path) -> Path | None:
    """Locate the config OpenCode discovers from ``workspace``.

    Workspace-owned config keeps precedence. When none exists, the explicit
    Nexus deployment config is the fallback, then the user-level config. The
    walk stops at the git root so a task worktree never inherits a parent project.
    """

    owned = workspace_owned_config(workspace)
    if owned is not None:
        return owned
    explicit = explicit_deployment_config()
    if explicit is not None:
        return explicit
    return global_config_path()