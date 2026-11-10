"""Worker 真实只读探测契约测试（mock HTTP + 真实 Git，仅本机）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app.worker_tools.inspection import (
    inspect_runtime,
    inspect_runtime_from_payload,
    skill_content_hash,
    status_summary,
)

CANARY = "sk-canary-secret-value"
URL_USERINFO = "git-user-info-canary"
URL_QUERY = "git-query-canary"
REMOTE = f"https://{URL_USERINFO}:{URL_USERINFO}@example.test/org/repo.git?token={URL_QUERY}"


@pytest.fixture
def opencode_server():
    state = {
        "health": (200, {"healthy": True, "version": "1.18.33"}),
        "session": (200, {}),
        "mcp": (200, {"nexus-library": {"status": "connected"}}),
        "skill": (200, [{"name": "alpha"}]),
        "config": (
            200,
            {
                "model": "custom-relay/route-1",
                "provider": {"custom-relay": {"options": {"baseURL": "{base}/gateway", "apiKey": CANARY}}},
            },
        ),
        "models": (200, {"data": [{"id": "route-1"}]}),
        "models_auth": None,
        "calls": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            current = self.server.state
            path = urlsplit(self.path).path
            current["calls"].append(path)
            if path == "/gateway/models":
                current["models_auth"] = self.headers.get("Authorization")
                status, body = current["models"]
            elif path == "/global/health":
                status, body = current["health"]
            elif path == "/session/status":
                status, body = current["session"]
            elif path == "/mcp":
                status, body = current["mcp"]
            elif path == "/skill":
                status, body = current["skill"]
            elif path == "/config":
                status, body = current["config"]
            else:
                status, body = 404, {"error": "not found"}
            raw = json.dumps(body).replace("{base}", f"http://{self.headers['Host']}")
            data = raw.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _repo(path: Path, *, remote: str, dirty: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-q")
    git("config", "user.email", "worker@example.test")
    git("config", "user.name", "Worker Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "init")
    git("remote", "add", "origin", remote)
    if dirty:
        (path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    return path


def _result_keys(result: dict) -> set[str]:
    return set(result)


def test_api_failure_is_not_healthy_even_when_port_listens(tmp_path: Path, opencode_server):
    state, base = opencode_server
    state["health"] = (503, {"healthy": False})
    workspace = _repo(tmp_path / "ws", remote="https://example.test/org/repo.git")

    result = inspect_runtime(base, str(workspace))

    assert "/global/health" in state["calls"]
    assert result["opencode"]["status"] == "unhealthy"
    assert result["opencode"]["version"] is None


def test_equipment_mismatch_is_reported_without_secrets(tmp_path: Path, opencode_server):
    state, base = opencode_server
    state["health"] = (200, {"healthy": True, "version": "1.18.34"})
    state["mcp"] = (
        200,
        {
            "nexus-library": {"status": "failed", "error": CANARY},
            "other": {"status": "connected"},
        },
    )
    state["skill"] = (200, [{"name": "other-skill"}])
    state["models"] = (401, {"error": CANARY})
    workspace = _repo(tmp_path / "ws", remote=REMOTE, dirty=True)

    result = inspect_runtime(
        base,
        str(workspace),
        expected_skills=["wanted-skill"],
        expected_mcp=["nexus-library"],
        expected_model="custom-relay/route-1",
        expected_opencode_version="1.18.33",
    )

    assert result["opencode"]["status"] == "unhealthy"
    assert result["opencode"]["version"] == "1.18.34"
    assert result["model"]["status"] == "healthy"
    assert result["model"]["configured"] is True
    assert result["model"]["model"] == "custom-relay/route-1"
    assert result["mcp"]["status"] == "unhealthy"
    assert result["mcp"]["connected"] is False
    assert result["mcp"]["missing"] == ["nexus-library"]
    assert result["skills"]["status"] == "unhealthy"
    assert result["skills"]["missing"] == ["wanted-skill"]
    assert result["repository"]["status"] == "healthy"
    assert result["repository"]["dirty"] is True
    assert result["repository"]["url"] == "https://example.test/org/repo.git"
    assert state["models_auth"] is None

    serialized = json.dumps(result, ensure_ascii=False)
    for secret in (CANARY, URL_USERINFO, URL_QUERY):
        assert secret not in serialized


def test_actual_model_differs_from_desired_is_rejected(tmp_path: Path, opencode_server):
    state, base = opencode_server
    state["config"] = (
        200,
        {
            "model": "custom-relay/route-2",
            "provider": {"custom-relay": {"options": {"baseURL": "{base}/gateway", "apiKey": CANARY}}},
        },
    )
    state["models"] = (
        200,
        {"data": [{"id": "route-1"}, {"id": "route-2"}]},
    )
    workspace = _repo(tmp_path / "ws", remote="https://example.test/org/repo.git")

    result = inspect_runtime(
        base,
        str(workspace),
        expected_model="custom-relay/route-1",
    )

    assert result["model"]["status"] == "unhealthy"
    assert result["model"]["model"] == "custom-relay/route-2"
    assert state["models_auth"] is None


def test_unicode_expected_names_are_not_silently_dropped(tmp_path: Path, opencode_server):
    state, base = opencode_server
    state["skill"] = (200, [{"name": "中文技能"}])
    workspace = _repo(tmp_path / "ws", remote="https://example.test/org/repo.git")

    healthy = inspect_runtime(base, str(workspace), expected_skills=["中文技能"])
    missing = inspect_runtime(base, str(workspace), expected_skills=["缺失技能"])

    assert healthy["skills"]["status"] == "healthy"
    assert healthy["skills"]["discovered"] == ["中文技能"]
    assert missing["skills"]["status"] == "unhealthy"
    assert missing["skills"]["missing"] == ["缺失技能"]
    with pytest.raises(ValueError):
        inspect_runtime_from_payload(base, str(workspace), {"expected_skills": ["ok", 3]})


def test_subdirectory_is_not_reported_as_repository_root(tmp_path: Path, opencode_server):
    _state, base = opencode_server
    root = _repo(tmp_path / "ws", remote="https://example.test/org/repo.git")
    child = root / "sub"
    child.mkdir()

    result = inspect_runtime(base, str(child))

    assert result["repository"]["status"] == "unhealthy"
    assert result["repository"]["error"] == "工作区不是 Git 仓库根目录"


def test_status_managed_by_only_when_unit_exists(monkeypatch, opencode_server):
    _state, base = opencode_server
    monkeypatch.delenv("NEXUS_WORKER_STATE_DIR", raising=False)
    assert status_summary(base)["managed_by"] == "external"
    monkeypatch.setenv("NEXUS_WORKER_STATE_DIR", "/opt/nexus/workers/env-1/state")
    assert status_summary(base)["managed_by"] == "supervisor"


def test_worker_artifact_imports_without_control_plane_runtime():
    root = Path(__file__).resolve().parents[2]
    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'app.runtime' or fullname.startswith('app.runtime.'):\n"
        "            raise ImportError('blocked control-plane runtime')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "from app.worker_tools import inspection\n"
        "assert inspection._local_config_path() is not None\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_expected_equipment_healthy_and_structure_is_stable(tmp_path: Path, opencode_server):
    state, base = opencode_server
    state["skill"] = (200, [{"name": "alpha"}, {"name": "beta"}])
    workspace = _repo(tmp_path / "ws", remote="https://example.test/org/repo.git")

    result = inspect_runtime(
        base,
        str(workspace),
        expected_skills=["alpha", "beta"],
        expected_mcp=["nexus-library"],
        expected_model="custom-relay/route-1",
        expected_opencode_version="v1.18.33",
    )

    assert _result_keys(result) == {
        "opencode",
        "model",
        "mcp",
        "skills",
        "repository",
        "checked_at",
        "spec_revision",
    }
    for section in ("opencode", "model", "mcp", "skills", "repository"):
        assert result[section]["status"] == "healthy"
    assert result["model"]["configured"] is True
    assert "authenticated" not in result["model"]
    assert result["mcp"]["missing"] == []
    assert result["skills"]["discovered"] == ["alpha", "beta"]
    assert result["repository"]["dirty"] is False
    assert result["repository"]["url"] == "https://example.test/org/repo.git"


@pytest.mark.parametrize("gateway_status", [401, 429, 503])
def test_gateway_failure_is_not_probed_by_worker_health(tmp_path, opencode_server, gateway_status):
    state, base = opencode_server
    state["models"] = (gateway_status, {"error": "temporarily unavailable"})
    workspace = _repo(tmp_path / "ws", remote="https://example.test/org/repo.git")
    result = inspect_runtime(base, str(workspace), expected_model="custom-relay/route-1")
    assert result["model"]["status"] == "healthy"
    assert result["model"]["configured"] is True
    assert "/gateway/models" not in state["calls"]
    assert "authenticated" not in result["model"]


def test_missing_gateway_credentials_still_blocks_configuration(tmp_path, opencode_server):
    state, base = opencode_server
    state["config"][1]["provider"]["custom-relay"]["options"].pop("apiKey")
    workspace = _repo(tmp_path / "ws", remote="https://example.test/org/repo.git")
    result = inspect_runtime(base, str(workspace), expected_model="custom-relay/route-1")
    assert result["model"]["status"] == "unhealthy"
    assert result["model"]["configured"] is False
    assert "/gateway/models" not in state["calls"]


def test_payload_cannot_supply_paths_urls_or_shell(tmp_path: Path, opencode_server):
    _state, base = opencode_server
    workspace = _repo(tmp_path / "ws", remote="https://example.test/org/repo.git")
    result = inspect_runtime_from_payload(base, str(workspace), {
        "expected_skills": ["alpha"], "expected_mcp": ["nexus-library"],
        "expected_opencode_version": "1.18.33", "workspace": "/nonexistent",
        "opencode_url": "http://127.0.0.1:1", "config_path": "/etc/passwd",
        "shell": "touch /tmp/should-not-run",
    })
    assert result is not None
    assert result["repository"]["url"] == "https://example.test/org/repo.git"
    assert result["opencode"]["status"] == "healthy"


def test_skill_body_hash_strips_frontmatter_and_ignores_empty_content():
    full = "---\nname: alpha\ndescription: demo\n---\n# Alpha\n\nBody\n"
    body = "# Alpha\n\nBody\n"

    assert skill_content_hash(full) == skill_content_hash(body)
    assert skill_content_hash(body) is not None
    assert skill_content_hash("") is None
    assert skill_content_hash("---\nname: empty\n---\n") is None
    assert skill_content_hash(None) is None


def test_effective_config_mcp_endpoints_and_skill_content_hashes(tmp_path: Path, opencode_server):
    state, base = opencode_server
    state["config"] = (
        200,
        {
            "model": "custom-relay/route-1",
            "provider": {"custom-relay": {"options": {"baseURL": "{base}/gateway", "apiKey": CANARY}}},
            "mcp": {
                "nexus-library": {
                    "type": "remote",
                    "url": "https://mcp-user:mcp-pass@mcp.example.test/mcp?token=mcp-canary",
                }
            },
        },
    )
    state["skill"] = (
        200,
        [
            {
                "name": "alpha",
                "description": "demo",
                "content": "---\nname: alpha\ndescription: demo\n---\n# Alpha\n\nBody\n",
            },
            {"name": "beta", "content": "# Beta\n"},
            {"name": "empty", "content": ""},
            {"name": "no-content"},
        ],
    )
    workspace = _repo(tmp_path / "ws", remote="https://example.test/org/repo.git")

    result = inspect_runtime(
        base,
        str(workspace),
        expected_skills=["alpha"],
        expected_mcp=["nexus-library"],
        expected_model="custom-relay/route-1",
    )

    assert result["mcp"]["status"] == "healthy"
    assert result["mcp"]["endpoints"] == {"nexus-library": "https://mcp.example.test/mcp"}
    assert result["skills"]["content_hashes"]["alpha"] == skill_content_hash("# Alpha\n\nBody\n")
    assert result["skills"]["content_hashes"]["beta"] == skill_content_hash("# Beta\n")
    assert result["skills"]["content_hashes"]["empty"] is None
    assert result["skills"]["content_hashes"]["no-content"] is None
    serialized = json.dumps(result, ensure_ascii=False)
    for secret in ("mcp-user", "mcp-pass", "mcp-canary"):
        assert secret not in serialized


def test_unreadable_effective_config_does_not_use_local_mcp_endpoints(
    tmp_path: Path, opencode_server, monkeypatch
):
    state, base = opencode_server
    local_config = tmp_path / "local-opencode.jsonc"
    local_config.write_text(
        json.dumps(
            {
                "mcp": {
                    "nexus-library": {
                        "type": "remote",
                        "url": "https://local.example.test/mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCODE_CONFIG", str(local_config))
    state["config"] = (503, {"error": "unavailable"})
    state["mcp"] = (200, {"nexus-library": {"status": "connected"}})
    state["skill"] = (200, [{"name": "alpha", "content": "# Alpha\n"}])
    workspace = _repo(tmp_path / "ws", remote="https://example.test/org/repo.git")

    result = inspect_runtime(base, str(workspace), expected_mcp=["nexus-library"])

    assert result["mcp"]["status"] == "healthy"
    assert result["mcp"]["endpoints"] is None
    assert result["model"]["status"] == "unknown"
    assert result["skills"]["content_hashes"]["alpha"] == skill_content_hash("# Alpha\n")
