from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import DeployConfig, parse_env_file

OPTIONAL_DIR = Path(__file__).resolve().parents[1] / "optional"


def llm_wiki_dependencies(config: DeployConfig) -> list[str]:
    missing: list[str] = []
    bundle = config.effective("LLM_WIKI_BUNDLE", "").strip()
    if not bundle:
        missing.append("LLM_WIKI_BUNDLE")
    elif not Path(bundle).expanduser().exists():
        missing.append(f"LLM_WIKI_BUNDLE 不存在：{bundle}")
    host = config.effective("LLM_WIKI_HOST", "").strip()
    if host:
        if shutil.which("ssh") is None:
            missing.append("ssh")
        if config.effective("LLM_WIKI_SSH_PASSWORD", "") and shutil.which("sshpass") is None:
            missing.append("sshpass")
    elif shutil.which("docker") is None:
        missing.append("docker（本机 LLM Wiki 部署需要）")
    chrome = any(
        shutil.which(name) for name in ("google-chrome", "chromium", "chromium-browser")
    )
    if not chrome:
        missing.append("Chrome/Chromium（ingest pump 需要）")
    return missing


def run_llm_wiki(config: DeployConfig) -> int:
    missing = llm_wiki_dependencies(config)
    if missing:
        print("[optional:llm-wiki] 缺少依赖：" + "; ".join(missing), file=sys.stderr)
        return 1
    _ensure_wiki_secrets(config)
    script = OPTIONAL_DIR / "llm_wiki.sh"
    if not script.is_file():
        print(f"[optional:llm-wiki] 缺少独立安装脚本：{script}", file=sys.stderr)
        return 1
    env = dict(os.environ)
    for path in (
        config.user_env_file,
        config.secrets_file,
        config.runtime_env_file,
        config.chat_model_file,
        config.optional_env_file,
    ):
        env.update(parse_env_file(path))
    result = subprocess.run(["bash", str(script)], env=env, check=False)
    return result.returncode


def remote_worker_dependencies(config: DeployConfig) -> list[str]:
    missing: list[str] = []
    if not config.effective("REMOTE_WORKER", "").strip():
        missing.append("--remote-worker HOST")
    if not config.effective("REMOTE_WORKER_SSH_PASSWORD", "").strip():
        missing.append("REMOTE_WORKER_SSH_PASSWORD")
    return missing


def _ensure_wiki_secrets(config: DeployConfig) -> None:
    path = config.secrets_file
    values = parse_env_file(path)
    changed = False
    if not values.get("ASTRA_LLM_WIKI_API_TOKEN"):
        values["ASTRA_LLM_WIKI_API_TOKEN"] = secrets.token_urlsafe(32)
        changed = True
    if not values.get("ASTRA_LLM_WIKI_PROJECT_ID"):
        values["ASTRA_LLM_WIKI_PROJECT_ID"] = str(uuid.uuid4())
        changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        from .configgen import env_line, own_generated_file

        path.write_text(
            "".join(env_line(key, value) for key, value in sorted(values.items())),
            encoding="utf-8",
        )
        own_generated_file(config, path, 0o600)


def _request(method: str, url: str, payload: dict | None = None) -> tuple[int, bytes]:
    request = Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urlopen(request, timeout=120) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()



def run_remote_worker(config: DeployConfig) -> int:
    missing = remote_worker_dependencies(config)
    if missing:
        print("[optional:remote-worker] 缺少参数：" + "; ".join(missing), file=sys.stderr)
        return 1
    base = f"http://127.0.0.1:{config.ports['task_api']}/api"
    worker = config.effective("REMOTE_WORKER", "")
    worker_id = config.effective("REMOTE_WORKER_ID", "nexus-remote")
    environment = {
        "mode": "LINUX_REMOTE",
        "name": worker_id,
        "host": worker,
        "port": int(config.effective("REMOTE_WORKER_PORT", "22")),
        "username": config.effective("REMOTE_WORKER_USER", "root"),
        "ssh_password": config.effective("REMOTE_WORKER_SSH_PASSWORD", ""),
        "opencode_server_port": int(
            config.effective("REMOTE_WORKER_OPENCODE_PORT", str(config.ports["opencode"]))
        ),
        "workspace_root": config.effective("REMOTE_WORKER_WORKSPACE", "/root/nexus-remote"),
        "model_name": f"astra-gateway/{config.openai_model}",
    }
    try:
        status, body = _request("GET", f"{base}/employees/{worker_id}")
        if status == 404:
            create = {
                "id": worker_id,
                "name": worker_id,
                "role": "remote worker",
                "description": "Nexus 远端 Worker",
                "execution_environment": environment,
            }
            status, body = _request("POST", f"{base}/employees", create)
        elif status == 200:
            status, body = _request(
                "PUT", f"{base}/employees/{worker_id}/environment", environment
            )
        if status not in {200, 201}:
            detail = body.decode("utf-8", errors="replace")[:300]
            print(f"[optional:remote-worker] 写入环境失败 HTTP {status}: {detail}", file=sys.stderr)
            return 1
        status, body = _request(
            "POST", f"{base}/employees/{worker_id}/environment/deploy", {}
        )
        if status != 200:
            detail = body.decode("utf-8", errors="replace")[:300]
            print(f"[optional:remote-worker] 部署请求失败 HTTP {status}: {detail}", file=sys.stderr)
            return 1
    except (URLError, OSError, ValueError) as exc:
        print(f"[optional:remote-worker] 部署失败：{exc}", file=sys.stderr)
        return 1
    result_event = None
    for line in body.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result_event = event
    if not result_event:
        print("[optional:remote-worker] 未收到 provisioning result 事件", file=sys.stderr)
        return 1
    if result_event.get("success") is not True:
        print(
            "[optional:remote-worker] 失败：" + str(result_event.get("message") or "unknown"),
            file=sys.stderr,
        )
        return 1
    print("[optional:remote-worker] ok")
    return 0
