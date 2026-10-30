#!/usr/bin/env python3
"""Initialize the Nexus installer environment without asking users to edit dotenv files.

Existing non-placeholder values are never overwritten. Password-bearing URLs are
entered with hidden input. This command may be called repeatedly by the installer.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "ops" / "env" / ".env.example"
DEFAULT_ENV = ROOT / "ops" / "env" / ".env"
REQUIRED = {
    "ASTRA_DATABASE_URL": "Nexus MySQL 连接 URL（mysql+asyncmy://用户:密码@主机:3306/nexus?charset=utf8mb4）",
    "AGENTSCOPE_SQL_URL": "AgentScope 独立 MySQL 连接 URL（mysql+asyncmy://用户:密码@主机:3306/nexus_agentscope?charset=utf8mb4）",
}
PLACEHOLDERS = ("REPLACE_ME", "replace-with", "example.internal", "your-password", "<本地")


def parse_env(path: Path) -> dict[str, str]:
    """Parse simple dotenv assignment lines; never execute an env file as code."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z_]\w*)=(.*)$", line)
        if not match:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        values[match.group(1)] = value
    return values


def missing(value: str | None) -> bool:
    return not value or any(token.lower() in value.lower() for token in PLACEHOLDERS)


def valid_url(key: str, value: str) -> bool:
    try:
        parts = urlsplit(value)
        if key in REQUIRED:
            return (parts.scheme == "mysql+asyncmy" and bool(parts.hostname)
                    and bool(parts.username) and bool(parts.password)
                    and bool(parts.path.strip("/")))
        if key == "ASTRA_REDIS_URL":
            return parts.scheme in ("redis", "rediss") and bool(parts.hostname)
        return True
    except ValueError:
        return False


def quoted(value: str) -> str:
    """Make an assignment safe for both Bash source and python-dotenv."""
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("配置值不能包含换行符或 NUL")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace(chr(96), "\\" + chr(96)) + '"'


def upsert(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    assignment = f"{key}={quoted(value)}"
    updated = []
    found = False
    for line in lines:
        if re.match(rf"^\s*{re.escape(key)}=", line):
            if not found:
                updated.append(assignment)
                found = True
        else:
            updated.append(line)
    if not found:
        updated.append(assignment)
    tmp = path.with_name(path.name + ".new")
    tmp.write_text("\n".join(updated) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def ask_url(key: str, description: str) -> str:
    if not sys.stdin.isatty():
        raise RuntimeError(f"缺少 {key}；请在受保护的环境配置中提供后重试（当前无交互终端）")
    while True:
        print(f"\n必填配置：{description}")
        value = getpass.getpass(f"{key}（输入时不显示）: ").strip()
        if valid_url(key, value):
            return value
        print(f"{key} 格式不正确，请重新输入。")


def main() -> int:
    parser = argparse.ArgumentParser(description="首次部署自动准备 Nexus 环境配置")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    args = parser.parse_args()
    dest = args.env_file.expanduser().resolve()
    if not dest.is_file():
        if not TEMPLATE.is_file():
            raise RuntimeError(f"环境模板不存在：{TEMPLATE}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATE, dest)
        os.chmod(dest, 0o600)
        print(f"[配置] 已初始化：{dest}")
        # A real one-command installation is a production install by default.
        upsert(dest, "ASTRA_ENVIRONMENT", "production")

    os.chmod(dest, 0o600)
    values = parse_env(dest)
    changed = []
    for key, description in REQUIRED.items():
        current = os.environ.get(key) or values.get(key)
        if not missing(current) and valid_url(key, current):
            continue
        new_value = ask_url(key, description)
        upsert(dest, key, new_value)
        values[key] = new_value
        changed.append(key)

    redis = os.environ.get("ASTRA_REDIS_URL") or values.get("ASTRA_REDIS_URL")
    if not missing(redis) and not valid_url("ASTRA_REDIS_URL", redis):
        raise RuntimeError("ASTRA_REDIS_URL 格式无效；请提供 redis:// 或 rediss:// 地址")
    if missing(redis):
        if sys.stdin.isatty():
            print("\nRedis 默认使用本机 redis://127.0.0.1:6379/1；外部 Redis 请提供完整 URL。")
            response = getpass.getpass("Redis URL（直接回车使用本机默认值）: ").strip()
            redis = response or "redis://127.0.0.1:6379/1"
        else:
            redis = "redis://127.0.0.1:6379/1"
        if not valid_url("ASTRA_REDIS_URL", redis):
            raise RuntimeError("Redis URL 格式无效")
        upsert(dest, "ASTRA_REDIS_URL", redis)
        changed.append("ASTRA_REDIS_URL")

    print("[配置] 必要连接参数已提供；数据库和 Redis 连通性将在部署预检中验证。")
    if changed:
        print("[配置] 新增/补全：" + "、".join(changed))
    else:
        print("[配置] 复用已有配置，未覆盖有效参数。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        raise SystemExit(1)
