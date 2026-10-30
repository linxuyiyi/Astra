"""Nexus validates and persists package defaults for the SSH deployment user."""

from __future__ import annotations

import configparser
import json
import shlex


def _pip_paths(home: str) -> list[str]:
    return ["/etc/pip.conf", home + "/.pip/pip.conf", home + "/.config/pip/pip.conf"]


async def environment(worker) -> dict:
    script = (
        "import os,json;print(json.dumps({k:v for k,v in os.environ.items() "
        "if k.upper().startswith(('PIP_','NPM_CONFIG_'))}))"
    )
    raw = await worker._command(shlex.join([worker._agent_interpreter(), "-I", "-c", script]))
    return json.loads(raw)


async def configure_sources(worker, manifest: dict, home: str, node_root: str) -> None:
    pip_index, registry = manifest["pip_index_url"].rstrip("/"), manifest["npm_registry"].rstrip("/")
    env = await environment(worker)
    for key, expected in [
        ("PIP_INDEX_URL", pip_index),
        ("NPM_CONFIG_REGISTRY", registry),
        ("npm_config_registry", registry),
    ]:
        if env.get(key) and env[key].rstrip("/") != expected:
            raise RuntimeError(f"{key} 与固定镜像冲突，已保留原配置，请管理员核对")
    for key in ("PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_TRUSTED_HOST"):
        if env.get(key):
            raise RuntimeError(f"{key} 引入额外来源或跳过 TLS 校验，请管理员核对")
    if env.get("PIP_CONFIG_FILE") == "/dev/null":
        raise RuntimeError("PIP_CONFIG_FILE 禁用了全局配置，请管理员核对")
    pip_paths = _pip_paths(home)
    if env.get("PIP_CONFIG_FILE"):
        pip_paths.append(env["PIP_CONFIG_FILE"])
    pip_found = npm_found = False
    for path in pip_paths:
        raw = await worker.ssh.read_file(str(path))
        if raw is None:
            continue
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(raw.decode())
        for section in config.sections():
            if any(
                config.get(section, key, fallback="").strip()
                for key in ("extra-index-url", "find-links", "trusted-host")
            ):
                raise RuntimeError(f"pip 配置含额外来源或跳过证书校验：{path}，未覆盖")
            value = config.get(section, "index-url", fallback="").strip()
            if value:
                if value.rstrip("/") != pip_index:
                    raise RuntimeError(f"pip 已有镜像与固定镜像冲突：{path}，未覆盖")
                pip_found = pip_found or section == "global"
    npm_paths = ["/etc/npmrc", node_root + "/etc/npmrc", home + "/.npmrc"]
    for key in (
        "NPM_CONFIG_USERCONFIG",
        "NPM_CONFIG_GLOBALCONFIG",
        "npm_config_userconfig",
        "npm_config_globalconfig",
    ):
        if env.get(key):
            npm_paths.append(env[key])
    for path in npm_paths:
        raw = await worker.ssh.read_file(path)
        if raw is None:
            continue
        for line in raw.decode().splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and (key.strip().lower() == "registry" or key.strip().endswith(":registry")):
                if value.strip().rstrip("/") != registry:
                    raise RuntimeError(f"npm 已有镜像与固定镜像冲突：{path}，未覆盖")
                npm_found = npm_found or key.strip().lower() == "registry"
    # Check the complete configuration before changing either file.
    await worker._ensure_safe()
    if not pip_found:
        path = home + "/.config/pip/pip.conf"
        raw = await worker.ssh.read_file(path)
        content = (raw or b"").decode()
        if "[global]" in content:
            content = content.replace("[global]", f"[global]\nindex-url = {pip_index}", 1)
        else:
            content += f"\n[global]\nindex-url = {pip_index}\n"
        await worker._write_atomic_if_changed(path, content.encode(), 0o600, backup=True)
    if not npm_found:
        path = home + "/.npmrc"
        raw = await worker.ssh.read_file(path)
        content = (raw or b"").decode() + f"\nregistry={registry}\n"
        await worker._write_atomic_if_changed(path, content.encode(), 0o600, backup=True)
