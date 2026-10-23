"""核对 npm ci 的固定装配，不依赖 ACP agentInfo 中的固定 0.0.1 文案。"""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import version
from pathlib import Path

from app.worker_agent.dsh_acp.profile import ACP_VERSION, DSH_VERSION


def inspect_installation(executable: Path, expected_lock: Path) -> dict:
    entry = executable.resolve(strict=True)
    package_root = entry.parent.parent
    metadata = json.loads((package_root / "package.json").read_text())
    if metadata.get("name") != "@deepseek-ai/dsh" or metadata.get("version") != DSH_VERSION:
        raise RuntimeError("dsh_package_version_mismatch")
    if version("agent-client-protocol") != ACP_VERSION:
        raise RuntimeError("acp_package_version_mismatch")
    modules = package_root.parent.parent
    if modules.name != "node_modules":
        raise RuntimeError("dsh_installation_layout_unverified")
    expected = json.loads(expected_lock.read_text())
    installed = json.loads((modules.parent / "package-lock.json").read_text())
    wanted = {path: item for path, item in expected["packages"].items() if path}
    actual = {path: item for path, item in installed["packages"].items() if path}
    if any(not path.startswith("node_modules/") or ".." in Path(path).parts for path in wanted):
        raise RuntimeError("npm_lock_paths_not_portable")
    if wanted != actual:
        raise RuntimeError("npm_dependency_lock_mismatch")
    versions = {}
    for relative, item in wanted.items():
        package = modules.parent / relative / "package.json"
        if not package.is_file():
            if item.get("optional"):
                continue
            raise RuntimeError("locked_dependency_missing")
        found = json.loads(package.read_text())
        if found.get("version") != item.get("version"):
            raise RuntimeError("installed_dependency_version_mismatch")
        versions[relative] = found.get("version")
    return {
        "dsh": DSH_VERSION, "agent_client_protocol": ACP_VERSION,
        "dependency_count": len(versions),
        "lock_sha256": hashlib.sha256(expected_lock.read_bytes()).hexdigest(),
        "installed_versions_sha256": hashlib.sha256(
            json.dumps(versions, sort_keys=True).encode(),
        ).hexdigest(),
    }
