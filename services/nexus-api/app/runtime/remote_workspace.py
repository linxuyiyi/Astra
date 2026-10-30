"""Server-side Git and managed-config decisions. The SSH target executes primitives only."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from pathlib import PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from app.worker_agent.jsonc import loads_jsonc


def merge_config(current: dict, manifest: dict, payload: dict) -> tuple[dict, dict]:
    import copy

    config = copy.deepcopy(current)
    if not isinstance(config, dict) or not isinstance(manifest, dict):
        raise RuntimeError("现有配置或管理清单不是对象，未覆盖")
    for key in ("provider", "mcp"):
        if key in config and not isinstance(config[key], dict):
            raise RuntimeError(f"现有 {key} 配置必须是对象，未覆盖")
        config.setdefault(key, {})
    managed_mcp = set(manifest.get("mcp", []))
    desired = {
        item["name"]: {"type": "remote", "url": item["url"], "enabled": True, "oauth": False}
        for item in payload.get("mcp_refs", [])
    }
    if payload.get("mcp"):
        desired["nexus-library"] = payload["mcp"]
    for name in managed_mcp - desired.keys():
        config["mcp"].pop(name, None)
    for name, value in desired.items():
        if (
            name in config["mcp"]
            and name not in managed_mcp
            and name != "nexus-library"
            and config["mcp"][name] != value
        ):
            raise RuntimeError(f"MCP 条目 {name} 已由其他来源管理，拒绝覆盖")
        config["mcp"][name] = value
    if payload.get("provider"):
        config["provider"]["nexus-gateway"] = payload["provider"]
    elif manifest.get("provider") == "nexus-gateway":
        config["provider"].pop("nexus-gateway", None)
    config.update(
        {
            "$schema": "https://opencode.ai/config.json",
            "model": payload["model"],
            "small_model": payload["model"],
            "enabled_providers": ["nexus-gateway"],
        }
    )
    skills = {}
    for item in payload.get("skills", []):
        name = item["name"]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name in {".", ".."}:
            raise RuntimeError("技能名称非法，拒绝写入")
        skills[name] = hashlib.sha256(item["body"].encode()).hexdigest()
    return config, {
        "provider": "nexus-gateway" if payload.get("provider") else None,
        "mcp": sorted(desired),
        "skills": skills,
    }


async def configure_runtime(worker, payload: dict) -> dict:
    path = payload["config_path"]
    root = str(PurePosixPath(path).parent)
    manifest_path = root + "/perenv.manifest.json"
    raw, previous = await worker.ssh.read_file(path), await worker.ssh.read_file(manifest_path)
    try:
        current = loads_jsonc(raw.decode()) if raw else {}
        manifest = json.loads(previous) if previous else {}
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("已有 OpenCode 配置或管理清单不是有效 JSON，请人工处理，未覆盖") from exc
    config, new_manifest = merge_config(current, manifest, payload)
    writes, removals = [], []
    desired = {item["name"]: item["body"].encode() for item in payload.get("skills", [])}
    for name in set(manifest.get("skills", {})) | desired.keys():
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name in {".", ".."}:
            raise RuntimeError("已有管理清单含非法技能名称，未修改")
        target = f"{root}/skill/{name}/SKILL.md"
        existing = await worker.ssh.read_file(target)
        if existing == desired.get(name):
            continue
        old_hash = manifest.get("skills", {}).get(name)
        if existing is not None and (old_hash is None or hashlib.sha256(existing).hexdigest() != old_hash):
            raise RuntimeError(f"技能 {name} 不受管理或被手工修改，拒绝覆盖/删除")
        if name in desired:
            writes.append((target, desired[name], existing))
        elif existing is not None:
            removals.append((target, existing))
    # Validate the entire change before the first config/skill write.
    await worker._ensure_safe()

    async def backup_skill(target, body):
        if body is not None:
            name = PurePosixPath(target).parent.name
            backup = f"{root}/.nexus-backups/{worker.operation_id}/skill-{name}.md"
            await worker._write_atomic_if_changed(backup, body, 0o600)

    for target, body, existing in writes:
        await backup_skill(target, existing)
        await worker._write_atomic_if_changed(target, body, 0o600)
    for target, existing in removals:
        await worker._ensure_safe()
        await backup_skill(target, existing)
        await worker._command(shlex.join(["rm", "-f", "--", target]))
        await worker._command(
            shlex.join(["rmdir", str(PurePosixPath(target).parent)]) + " 2>/dev/null || true"
        )
    changed = config != current
    if changed:
        await worker._write_atomic_if_changed(
            path, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode(), 0o600, backup=True
        )
    if manifest != new_manifest:
        await worker._write_atomic_if_changed(manifest_path, json.dumps(new_manifest).encode(), 0o600)
    return {"configuration": changed, "skills": bool(writes or removals)}


async def git_workspace(worker, *, url: str, branch: str, token_path: str = "") -> tuple[dict, bool]:
    workspace = worker.environment.workspace_root
    version = await worker._command("git --version")
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", version)
    if not match or tuple(int(x or 0) for x in match.groups()) < (2, 18, 0):
        raise RuntimeError("Git 版本低于 2.18，请管理员升级；不自动覆盖系统 Git")
    await worker._ensure_safe()
    top = await worker._command(
        shlex.join(["git", "-C", workspace, "rev-parse", "--show-toplevel"]) + " 2>/dev/null || true"
    )
    real = await worker._command(
        shlex.join(
            [
                worker._agent_interpreter(),
                "-I",
                "-c",
                "import os,sys;print(os.path.realpath(sys.argv[1]))",
                workspace,
            ]
        )
    )
    changed, remote_commit = False, None
    if top and top != real:
        raise RuntimeError("工程目录不是 Git 工作树顶层，已停止并保留现场")

    def git(*args):
        return shlex.join(["git", "-C", workspace, *args])

    askpass = token_path + ".askpass" if token_path else ""
    try:
        if askpass:
            await worker._command(shlex.join(["chmod", "600", token_path]))
            await worker.ssh.write_file(
                askpass,
                (
                    "#!/bin/sh\ncase \"$1\" in\n*Username*) printf 'git\\n';;\n*) cat "
                    + shlex.quote(token_path)
                    + ";;\nesac\n"
                ).encode(),
                mode=0o700,
            )
        auth = "GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=" + shlex.quote(askpass) + " "
        if top:
            if url:
                if await worker._command(git("config", "--get", "remote.origin.url") + " || true") != url:
                    raise RuntimeError("工程目录的 origin 与绑定仓库不一致，已停止并保留现场")
                if (
                    await worker._command(git("symbolic-ref", "--quiet", "--short", "HEAD") + " || true")
                    != branch
                ):
                    raise RuntimeError("工程目录当前分支与绑定分支不一致，已停止并保留现场")
        else:
            if worker.action == "update":
                raise RuntimeError("工程目录不是 Git 仓库，无法执行更新")
            contents = await worker._command(
                f"if test -e {shlex.quote(workspace)}; then find {shlex.quote(workspace)} "
                "-mindepth 1 -maxdepth 1 -print -quit; fi"
            )
            if contents:
                raise RuntimeError("工程目录不是 Git 仓库且不为空，已保留现场")
            await worker._command(shlex.join(["mkdir", "-p", str(PurePosixPath(workspace).parent)]))
            if url:
                await worker._command(
                    auth
                    + shlex.join(
                        [
                            "git",
                            "-c",
                            "credential.helper=",
                            "clone",
                            "--branch",
                            branch,
                            "--",
                            url,
                            workspace,
                        ]
                    ),
                    timeout=180,
                )
            else:
                await worker._command(shlex.join(["mkdir", "-p", workspace]))
                await worker._command(git("init", "-q"))
                await worker._command(
                    git(
                        "-c",
                        "user.name=Nexus",
                        "-c",
                        "user.email=nexus@localhost",
                        "commit",
                        "-q",
                        "--allow-empty",
                        "-m",
                        "chore: initialize worker workspace",
                    )
                )
            changed = True
        head = await worker._command(git("rev-parse", "--verify", "HEAD"))
        if worker.action == "update":
            if not url:
                raise RuntimeError("update 需要绑定仓库")
            if await worker._command(git("status", "--porcelain", "--untracked-files=normal")):
                raise RuntimeError("工程仓库存在未提交或未跟踪变更，已停止更新并保留现场")
            await worker._command(
                auth + git("-c", "credential.helper=", "fetch", "--no-tags", "--", "origin", branch),
                timeout=180,
            )
            if await worker._command(git("status", "--porcelain", "--untracked-files=normal")):
                raise RuntimeError("获取远端后工作区出现变更，已停止更新并保留现场")
            remote_commit = await worker._command(git("rev-parse", "--verify", "FETCH_HEAD^{commit}"))
            if head != remote_commit:
                can_ff = await worker._command(
                    git("merge-base", "--is-ancestor", head, remote_commit) + " && printf yes || printf no"
                )
                if can_ff != "yes":
                    raise RuntimeError("本地分支有未推送提交或已分叉，已拒绝更新并保留现场")
                await worker._ensure_safe()
                await worker._command(git("merge", "--ff-only", "--no-edit", remote_commit))
                changed = True
        final = await worker._command(git("rev-parse", "HEAD"))
        if remote_commit and final != remote_commit:
            raise RuntimeError("更新后的提交与远端目标提交不一致")
        parsed = urlsplit(url)
        safe_url = (
            urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", "")) if parsed.scheme else url
        )
        return {
            "url": safe_url,
            "branch": branch,
            "commit": final,
            "dirty": bool(await worker._command(git("status", "--porcelain", "--untracked-files=normal"))),
            "remote_commit": remote_commit,
        }, changed
    finally:
        if token_path:
            await worker._command(shlex.join(["rm", "-f", "--", token_path, askpass]))
