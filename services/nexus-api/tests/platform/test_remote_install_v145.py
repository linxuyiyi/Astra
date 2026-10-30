"""Server-owned config and real Git deployment/update contract tests."""

import asyncio
import json
import subprocess
import time
from pathlib import Path

from app.runtime.remote_workspace import configure_runtime, git_workspace
from tests.platform.deployment_target import LocalTarget


def _run_config(tmp_path, payload, operation="op1"):
    worker = LocalTarget(tmp_path)
    worker.operation_id = operation
    result_file = tmp_path / "result.json"
    try:
        result = asyncio.run(configure_runtime(worker, payload))
        result_file.write_text(json.dumps(result))
        return subprocess.CompletedProcess([], 0, "", ""), result_file
    except Exception as exc:
        return subprocess.CompletedProcess([], 1, "", str(exc)), result_file


def _run_repository(tmp_path, *, action, repo_url, workspace, branch="main", token_file=""):
    worker = LocalTarget(tmp_path, workspace=workspace, action=action)
    try:
        result, changed = asyncio.run(
            git_workspace(worker, url=repo_url, branch=branch, token_path=token_file)
        )
        output = "RESULT\t" + str(changed).lower() + "\t" + (result["remote_commit"] or "") + "\n"
        return subprocess.CompletedProcess([], 0, output, "")
    except Exception as exc:
        return subprocess.CompletedProcess([], 1, "", str(exc))


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


def _repo_result(output: str) -> tuple[str, str]:
    line = next(item for item in output.splitlines() if item.startswith("RESULT\t"))
    _, changed, remote = line.split("\t")
    return changed, remote


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


def test_deploy_clone_then_update_fast_forwards(tmp_path):
    bare, seed = _make_remote(tmp_path)
    workspace = tmp_path / "workspace"
    first = _run_repository(tmp_path, action="deploy", repo_url=str(bare), workspace=workspace)
    assert first.returncode == 0, first.stderr
    assert _repo_result(first.stdout)[0] == "true"
    first_commit = _git(workspace, "rev-parse", "HEAD").stdout.strip()

    target = _advance(seed, "v2\n", "second")
    deploy_again = _run_repository(tmp_path, action="deploy", repo_url=str(bare), workspace=workspace)
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
