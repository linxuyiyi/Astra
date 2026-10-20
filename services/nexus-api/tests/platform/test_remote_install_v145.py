"""v1.4.5 remote installer: Git semantics, config idempotence and safety probes."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "app/runtime/remote_install.sh"
JSONC = ROOT / "app/worker_agent/jsonc.py"
NODE = shutil.which("node")


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _functions() -> str:
    text = _script()
    return text.split("# >>> nexus-installer-functions\n", 1)[1].split(
        "# <<< nexus-installer-functions", 1
    )[0]


def _run_functions(
    body: str, *, timeout: int = 30, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    source = (
        "set -euo pipefail\n"
        + _functions()
        + "\npython_bin="
        + shlex.quote(sys.executable)
        + "\n"
        + body
    )
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", source],
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def _git(cwd: Path | None, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["git"]
    if cwd is not None:
        command += ["-C", str(cwd)]
    return subprocess.run(command + list(args), text=True, capture_output=True, check=check)


def _make_remote(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "remote.git"
    _git(None, "init", "--bare", str(bare))
    _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")
    seed = tmp_path / "seed"
    _git(None, "clone", str(bare), str(seed))
    _git(seed, "checkout", "-b", "main")
    (seed / "app.txt").write_text("v1\n", encoding="utf-8")
    _git(seed, "add", "app.txt")
    _git(
        seed,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "initial",
    )
    _git(seed, "push", "-u", "origin", "main")
    return bare, seed


def _advance(seed: Path, text: str, message: str) -> str:
    (seed / "app.txt").write_text(text, encoding="utf-8")
    _git(seed, "add", "app.txt")
    _git(
        seed,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        message,
    )
    _git(seed, "push", "origin", "main")
    return _git(seed, "rev-parse", "HEAD").stdout.strip()


def _run_repository(
    tmp_path: Path,
    *,
    action: str,
    repo_url: str,
    workspace: Path,
    branch: str = "main",
    token_file: str = "",
) -> subprocess.CompletedProcess[str]:
    body = f"""
workspace={shlex.quote(str(workspace))}
workspace_real=$("$python_bin" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$workspace")
action={shlex.quote(action)}
repo_url={shlex.quote(repo_url)}
repo_branch={shlex.quote(branch)}
token_file={shlex.quote(token_file)}
repo_changed=false
remote_commit=""
repository_stage
printf 'RESULT\\t%s\\t%s\\n' "$repo_changed" "$remote_commit"
"""
    return _run_functions(body)


def _repo_result(output: str) -> tuple[str, str]:
    line = next(item for item in output.splitlines() if item.startswith("RESULT\t"))
    _, changed, remote = line.split("\t")
    return changed, remote


def test_install_script_has_valid_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_install_script_uses_only_nexus_identifiers():
    text = _script()
    assert "astra" not in text.lower()
    assert "NEXUS_STEP\\t" in text
    assert "NEXUS_META\\t" in text
    assert "npm config" not in text
    assert "/usr/local/bin/node" not in text
    assert "/usr/local/bin/npm" not in text
    assert "$HOME/.local/bin" not in text


def _payload(config: Path, **overrides) -> dict:
    payload = {
        "config_path": str(config),
        "model": "nexus-gateway/model-a",
        "provider": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Nexus API 中转",
            "options": {"baseURL": "http://127.0.0.1:8110/v1", "apiKey": "agw_secret"},
            "models": {"model-a": {"name": "model-a"}},
        },
        "mcp": {"type": "remote", "url": "http://127.0.0.1:8201/mcp", "enabled": True},
        "mcp_refs": [{"name": "worker-mcp", "url": "http://127.0.0.1:9/mcp"}],
        "skills": [{"name": "alpha", "body": "# alpha v1\n"}],
    }
    payload.update(overrides)
    return payload


def _run_config(tmp_path: Path, payload: dict, operation: str = "op1"):
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(payload), encoding="utf-8")
    result_file = tmp_path / "result.json"
    source = _script().split("<<'NODE'\n", 1)[1].split("\nNODE", 1)[0]
    result = subprocess.run(
        [
            NODE,
            "-",
            str(payload_file),
            operation,
            sys.executable,
            str(JSONC),
            str(result_file),
        ],
        input=source,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, result_file


@pytest.mark.skipif(not NODE, reason="Node.js required for config merge")
def test_config_merge_is_scoped_and_idempotent(tmp_path):
    config = tmp_path / "opencode.jsonc"
    original = {
        "provider": {"custom": {"options": {"apiKey": "private"}}},
        "mcp": {"custom-mcp": {"enabled": False}},
        "permission": {"bash": "ask"},
    }
    config.write_text("// keep this\n" + json.dumps(original), encoding="utf-8")
    (tmp_path / "skill" / "user-skill").mkdir(parents=True)
    (tmp_path / "skill" / "user-skill" / "SKILL.md").write_text("user", encoding="utf-8")
    payload = _payload(config)

    result, result_file = _run_config(tmp_path, payload, "op1")
    assert result.returncode == 0, result.stderr
    merged = json.loads(config.read_text(encoding="utf-8"))
    assert merged["provider"]["custom"] == original["provider"]["custom"]
    assert merged["provider"]["nexus-gateway"]["options"]["baseURL"] == "http://127.0.0.1:8110/v1"
    assert "astra-gateway" not in merged["provider"]
    assert merged["mcp"]["custom-mcp"] == original["mcp"]["custom-mcp"]
    assert merged["mcp"]["worker-mcp"]["url"] == "http://127.0.0.1:9/mcp"
    assert merged["mcp"]["nexus-library"]["url"] == "http://127.0.0.1:8201/mcp"
    assert merged["permission"] == original["permission"]
    assert config.stat().st_mode & 0o777 == 0o600
    backup = tmp_path / "opencode.jsonc.nexus-backup-op1"
    assert backup.exists()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert json.loads(backup.read_text().split("\n", 1)[1]) == original
    assert json.loads(result_file.read_text()) == {"configuration": True, "skills": True}
    manifest = json.loads((tmp_path / "perenv.manifest.json").read_text())
    assert manifest["provider"] == "nexus-gateway"
    assert set(manifest["mcp"]) == {"nexus-library", "worker-mcp"}
    assert set(manifest["skills"]) == {"alpha"}
    assert (tmp_path / "skill" / "alpha" / "SKILL.md").read_text() == "# alpha v1\n"
    assert (tmp_path / "skill" / "user-skill" / "SKILL.md").read_text() == "user"

    config_inode = config.stat().st_ino
    config_mtime = config.stat().st_mtime_ns
    skill_mtime = (tmp_path / "skill" / "alpha" / "SKILL.md").stat().st_mtime_ns
    manifest_mtime = (tmp_path / "perenv.manifest.json").stat().st_mtime_ns
    time.sleep(0.01)
    second, second_result = _run_config(tmp_path, payload, "op2")
    assert second.returncode == 0, second.stderr
    assert json.loads(second_result.read_text()) == {"configuration": False, "skills": False}
    assert not (tmp_path / "opencode.jsonc.nexus-backup-op2").exists()
    assert config.stat().st_ino == config_inode
    assert config.stat().st_mtime_ns == config_mtime
    assert (tmp_path / "skill" / "alpha" / "SKILL.md").stat().st_mtime_ns == skill_mtime
    assert (tmp_path / "perenv.manifest.json").stat().st_mtime_ns == manifest_mtime

    changed = _payload(
        config,
        mcp_refs=[],
        skills=[
            {"name": "alpha", "body": "# alpha v2\n"},
            {"name": "beta", "body": "# beta\n"},
        ],
    )
    third, third_result = _run_config(tmp_path, changed, "op3")
    assert third.returncode == 0, third.stderr
    assert json.loads(third_result.read_text()) == {"configuration": True, "skills": True}
    merged = json.loads(config.read_text(encoding="utf-8"))
    assert "worker-mcp" not in merged["mcp"]
    assert "nexus-library" in merged["mcp"]
    assert merged["mcp"]["custom-mcp"] == original["mcp"]["custom-mcp"]
    assert (tmp_path / "skill" / "alpha" / "SKILL.md").read_text() == "# alpha v2\n"
    assert (tmp_path / "skill" / "beta" / "SKILL.md").read_text() == "# beta\n"
    assert (tmp_path / "skill" / "user-skill" / "SKILL.md").read_text() == "user"

    removed = _payload(config, mcp=None, mcp_refs=[], provider=None, skills=[])
    removed["model"] = "opencode/free"
    fourth, fourth_result = _run_config(tmp_path, removed, "op4")
    assert fourth.returncode == 0, fourth.stderr
    assert json.loads(fourth_result.read_text()) == {"configuration": True, "skills": True}
    merged = json.loads(config.read_text(encoding="utf-8"))
    assert "nexus-gateway" not in merged["provider"]
    assert merged["provider"]["custom"] == original["provider"]["custom"]
    assert "nexus-library" not in merged["mcp"]
    assert merged["mcp"]["custom-mcp"] == original["mcp"]["custom-mcp"]
    assert not (tmp_path / "skill" / "alpha").exists()
    assert not (tmp_path / "skill" / "beta").exists()
    assert (tmp_path / "skill" / "user-skill" / "SKILL.md").read_text() == "user"


@pytest.mark.skipif(not NODE, reason="Node.js required for config merge")
def test_config_merge_refuses_unmanaged_conflicts_and_invalid_json(tmp_path):
    config = tmp_path / "opencode.jsonc"
    config.write_text(
        json.dumps(
            {
                "provider": {"custom": {}},
                "mcp": {"worker-mcp": {"url": "http://user.invalid/mcp"}, "keep": {}},
            }
        ),
        encoding="utf-8",
    )
    payload = _payload(config)
    result, _ = _run_config(tmp_path, payload, "op1")
    assert result.returncode != 0
    assert "拒绝覆盖" in result.stderr
    assert json.loads(config.read_text())["mcp"]["worker-mcp"]["url"] == "http://user.invalid/mcp"
    assert not (tmp_path / "opencode.jsonc.nexus-backup-op1").exists()

    broken = tmp_path / "broken.jsonc"
    broken.write_text("not json", encoding="utf-8")
    result, _ = _run_config(tmp_path, _payload(broken), "op2")
    assert result.returncode != 0
    assert "不是有效 JSON" in result.stderr
    assert broken.read_text() == "not json"


def test_native_idle_allows_provably_stopped_server(tmp_path):
    import socket

    workspace = tmp_path / "ws"
    workspace.mkdir()
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    result = _run_functions(
        f"workspace={shlex.quote(str(workspace))}\nassert_native_idle {port}\n"
    )
    assert result.returncode == 0, result.stderr


def _serve(body: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.mark.parametrize(
    ("body", "returncode", "message"),
    [
        (b"{}", 0, ""),
        (b'{"ses": {"type": "idle"}}', 0, ""),
        (b'{"ses": {"type": "busy"}}', 4, "正在执行"),
        (b'{"ses": {"type": "retry"}}', 4, "正在执行"),
        (b'{"ses": {}}', 3, "无法确认"),
        (b'{"ses": {"type": "unknown"}}', 3, "无法确认"),
        (b'{"ses": {"type": null}}', 3, "无法确认"),
        (b'{"ses": null}', 3, "无法确认"),
        (b'{"ses": "idle"}', 3, "无法确认"),
        (b"not-json", 3, "无法确认"),
        (b"[]", 3, "无法确认"),
    ],
)
def test_native_idle_handles_busy_idle_and_malformed_probes(
    tmp_path, body, returncode, message
):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    server = _serve(body)
    try:
        result = _run_functions(
            f"workspace={shlex.quote(str(workspace))}\nassert_native_idle {server.server_port}\n"
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == returncode
    if message:
        assert message in result.stderr


def test_native_idle_requires_an_explicit_port(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = _run_functions(
        f"workspace={shlex.quote(str(workspace))}\nassert_native_idle ''\n"
    )
    assert result.returncode != 0
    assert "缺少 opencode_server_port" in result.stderr


def test_native_idle_rejects_unknown_socket_errors(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "sitecustomize.py").write_text(
        "import socket as _socket\n"
        "class _Probe(_socket.socket):\n"
        "    def connect(self, address):\n"
        "        raise TimeoutError('timed out')\n"
        "_socket.socket = _Probe\n",
        encoding="utf-8",
    )
    existing = os.environ.get("PYTHONPATH", "")
    python_path = str(tmp_path) + (os.pathsep + existing if existing else "")
    result = _run_functions(
        f"workspace={shlex.quote(str(workspace))}\nassert_native_idle 4096\n",
        extra_env={"PYTHONPATH": python_path, "PYTHONNOUSERSITE": "1"},
    )
    assert result.returncode == 3
    assert "无法确认 OpenCode 监听状态" in result.stderr


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("1", True),
        ("4096", True),
        ("65535", True),
        ("0", False),
        ("65536", False),
        ("", False),
        ("latest", False),
        (" 4096", False),
    ],
)
def test_port_validator(value, valid):
    result = _run_functions(f"validate_port {shlex.quote(value)}\n")
    assert (result.returncode == 0) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("22.23.3", True),
        ("1.18.35", True),
        ("1.18.35-beta.1", True),
        ("1.18.35+build.7", True),
        ("1.18.35-rc.1+build.7", True),
        ("latest", False),
        ("v1.18.35", False),
        ("1.18", False),
        ("1.18.35/evil", False),
        ("https://example.invalid/opencode.tgz", False),
        ("", False),
    ],
)
def test_semver_validator(value, valid):
    result = _run_functions(f"validate_semver {shlex.quote(value)}\n")
    assert (result.returncode == 0) is valid


def test_deploy_clone_then_update_fast_forwards(tmp_path):
    bare, seed = _make_remote(tmp_path)
    workspace = tmp_path / "workspace"
    first = _run_repository(tmp_path, action="deploy", repo_url=str(bare), workspace=workspace)
    assert first.returncode == 0, first.stderr
    assert _repo_result(first.stdout)[0] == "true"
    first_commit = _git(workspace, "rev-parse", "HEAD").stdout.strip()

    target = _advance(seed, "v2\n", "second")
    deploy_again = _run_repository(
        tmp_path, action="deploy", repo_url=str(bare), workspace=workspace
    )
    assert deploy_again.returncode == 0, deploy_again.stderr
    assert _repo_result(deploy_again.stdout)[0] == "false"
    assert _git(workspace, "rev-parse", "HEAD").stdout.strip() == first_commit

    update = _run_repository(tmp_path, action="update", repo_url=str(bare), workspace=workspace)
    assert update.returncode == 0, update.stderr
    changed, remote = _repo_result(update.stdout)
    assert changed == "true"
    assert remote == target
    assert _git(workspace, "rev-parse", "HEAD").stdout.strip() == target


def test_update_refuses_dirty_untracked_wrong_branch_origin_ahead_and_diverged(tmp_path):
    bare, seed = _make_remote(tmp_path)
    workspace = tmp_path / "workspace"
    assert _run_repository(tmp_path, action="deploy", repo_url=str(bare), workspace=workspace).returncode == 0
    head = _git(workspace, "rev-parse", "HEAD").stdout.strip()

    (workspace / "app.txt").write_text("local edit\n", encoding="utf-8")
    dirty = _run_repository(tmp_path, action="update", repo_url=str(bare), workspace=workspace)
    assert dirty.returncode != 0
    assert "未提交或未跟踪变更" in dirty.stderr
    assert _git(workspace, "rev-parse", "HEAD").stdout.strip() == head
    assert (workspace / "app.txt").read_text() == "local edit\n"
    _git(workspace, "checkout", "--", "app.txt")

    (workspace / "notes.txt").write_text("keep me\n", encoding="utf-8")
    untracked = _run_repository(tmp_path, action="update", repo_url=str(bare), workspace=workspace)
    assert untracked.returncode != 0
    assert "未提交或未跟踪变更" in untracked.stderr
    assert (workspace / "notes.txt").read_text() == "keep me\n"
    (workspace / "notes.txt").unlink()

    _git(workspace, "checkout", "-b", "feature")
    wrong_branch = _run_repository(tmp_path, action="update", repo_url=str(bare), workspace=workspace)
    assert wrong_branch.returncode != 0
    assert "当前分支与绑定分支不一致" in wrong_branch.stderr
    _git(workspace, "checkout", "main")

    other = tmp_path / "other.git"
    _git(None, "init", "--bare", str(other))
    _git(workspace, "remote", "set-url", "origin", str(other))
    wrong_origin = _run_repository(tmp_path, action="update", repo_url=str(bare), workspace=workspace)
    assert wrong_origin.returncode != 0
    assert "origin 与绑定仓库不一致" in wrong_origin.stderr
    _git(workspace, "remote", "set-url", "origin", str(bare))

    (workspace / "local.txt").write_text("local\n", encoding="utf-8")
    _git(workspace, "add", "local.txt")
    _git(
        workspace,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "local",
    )
    ahead = _run_repository(tmp_path, action="update", repo_url=str(bare), workspace=workspace)
    assert ahead.returncode != 0
    assert "未推送提交" in ahead.stderr
    assert (workspace / "local.txt").read_text() == "local\n"

    _advance(seed, "remote diverged\n", "diverged")
    diverged = _run_repository(tmp_path, action="update", repo_url=str(bare), workspace=workspace)
    assert diverged.returncode != 0
    assert "已分叉" in diverged.stderr


def test_repository_rejects_non_top_level_workspace(tmp_path):
    bare, _ = _make_remote(tmp_path)
    root = tmp_path / "root"
    assert _run_repository(tmp_path, action="deploy", repo_url=str(bare), workspace=root).returncode == 0
    nested = root / "sub"
    nested.mkdir()
    result = _run_repository(tmp_path, action="deploy", repo_url=str(bare), workspace=nested)
    assert result.returncode != 0
    assert "不是 Git 工作树顶层" in result.stderr


def test_clone_consumes_and_removes_one_time_token(tmp_path):
    bare, _ = _make_remote(tmp_path)
    token = tmp_path / "token"
    token.write_text("ghp_private_token_value", encoding="utf-8")
    token.chmod(0o600)
    workspace = tmp_path / "workspace"
    result = _run_repository(
        tmp_path,
        action="deploy",
        repo_url=str(bare),
        workspace=workspace,
        token_file=str(token),
    )
    assert result.returncode == 0, result.stderr
    assert not token.exists()
    assert "ghp_private_token_value" not in result.stdout + result.stderr
