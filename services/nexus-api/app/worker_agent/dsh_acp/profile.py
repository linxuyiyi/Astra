"""从官方 ACP profile 派生仅经过 Nexus Gateway 的装配，不包含厂商凭据。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

DSH_VERSION = "0.2.1-rc.2"
ACP_VERSION = "0.8.1"
PROVIDER = "nexus-gateway"
CREDENTIAL_REF = "NEXUS_DSH_GATEWAY_KEY"


@dataclass(frozen=True)
class GatewayModel:
    base_url: str
    alias: str
    context_window: int
    max_tokens: int
    thinking_format: str | None = None

    def __post_init__(self) -> None:
        url = urlsplit(self.base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path.rstrip("/") != "/v1"
        ):
            raise ValueError("必须明确配置 Nexus Gateway /v1 地址，不接受凭据、查询参数或厂商默认值")
        if not self.alias.strip() or self.context_window <= 0 or self.max_tokens <= 0:
            raise ValueError("模型别名、上下文与输出上限必须显式配置")
        if self.thinking_format not in {None, "deepseek", "zai", "qwen"}:
            raise ValueError("未知 thinking format；必须对照固定版 pi-ai 契约")


def profile_patch(home: Path, model: GatewayModel) -> list[dict]:
    declared = {
        "id": model.alias,
        "name": model.alias,
        "contextWindow": model.context_window,
        "maxTokens": model.max_tokens,
        "input": ["text"],
    }
    provider = {
        "apiKeyEnv": CREDENTIAL_REF,
        "api": "openai-completions",
        "baseURL": model.base_url.rstrip("/"),
        "models": [declared],
        "retryPolicy": {"mode": "normal", "maxRetries": 0},
    }
    if model.thinking_format:
        provider["compat"] = {"thinkingFormat": model.thinking_format}
    # 移除官方 profile 中可直连或动态装配的入口；不声明网络隔离已完成。
    disabled = (
        "llm-deepseek",
        "llm-deepseek-account",
        "deepseek-account",
        "deepseek-llm-api-extensions",
        "web-search-deepseek",
        "web-deepseek-api",
        "web-fetch-http",
        "tool-web",
        "plugin-manager",
        "tool-plugin-manager",
        "config-editor",
        "settings",
        "hmr",
        "session-title-llm",
        "session-telemetry-otel",
        "command-feedback",
    )
    return [
        *({"id": name, "disabled": True} for name in disabled),
        {"id": "llm-pi-ai", "config": {"providers": {PROVIDER: provider}}},
        {"id": "agent-default-model", "config": {"provider": PROVIDER, "model": model.alias}},
        {"id": "acp", "config": {"provider": PROVIDER, "model": model.alias}},
        {"id": "credentials", "config": {"path": str(home / ".credentials.yaml"), "watch": False}},
        {
            "id": "session-persistence-jsonl",
            "config": {"root": str(home / "sessions"), "compression": "none"},
        },
        {
            "id": "skill-filesystem",
            "config": {
                "includeDefaultRoots": False,
                "customSkillDirs": [str(home / "skills")],
                "watch": False,
            },
        },
        {"id": "approval", "config": {"policy": "ask"}},
        {"id": "sandbox-policy", "config": {"mode": "workspace-write"}},
    ]


def private_write(path: Path, content: str) -> None:
    """拒绝跟随链接和覆盖既有文件；只有显式的新绑定可创建配置。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def prepare_profile(home: Path, model: GatewayModel, gateway_key: str) -> tuple[Path, str]:
    if not home.is_absolute() or home.is_symlink():
        raise ValueError("DSH_HOME 必须是独立绝对目录")
    if not gateway_key.strip():
        raise ValueError("缺少已登记的 Nexus Gateway 调用密钥")
    # 不接管旧 DSH_HOME，不覆盖人工配置或正在写入的 Session。
    home.mkdir(mode=0o700, parents=False, exist_ok=False)
    (home / "skills").mkdir(mode=0o700)
    patch = profile_patch(home.resolve(), model)
    digest = hashlib.sha256(json.dumps(patch, sort_keys=True).encode()).hexdigest()
    path = home / "nexus-gateway.patch.yml"
    private_write(path, yaml.safe_dump(patch, allow_unicode=True, sort_keys=False))
    private_write(
        home / ".credentials.yaml",
        yaml.safe_dump(
            {
                "version": 1,
                "refs": {CREDENTIAL_REF: gateway_key},
                "records": {},
            }
        ),
    )
    return path, digest
