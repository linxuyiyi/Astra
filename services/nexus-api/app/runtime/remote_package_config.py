"""配置 SSH 用户的包来源，保留现有 npm 设置。"""

from __future__ import annotations

import configparser
import io
import json
import shlex

from app.config.worker_deployment import pip_trusted_host, source_url


def _pip_paths(home: str) -> list[str]:
    return ["/etc/pip.conf", home + "/.pip/pip.conf", home + "/.config/pip/pip.conf"]


async def environment(worker) -> dict:
    script = (
        "import os,json;print(json.dumps({k:v for k,v in os.environ.items() "
        "if k.upper().startswith(('PIP_','NPM_CONFIG_'))}))"
    )
    raw = await worker._command(shlex.join([worker._agent_interpreter(), "-I", "-c", script]))
    return json.loads(raw)


def _check_trusted(value: str, allowed: str, location: str) -> bool:
    hosts = {item.lower() for item in value.split()}
    if hosts and (not allowed or hosts != {allowed}):
        raise RuntimeError(f"pip 可信主机与固定镜像冲突或跳过证书校验：{location}，未覆盖")
    return bool(hosts)


async def configure_sources(worker, manifest: dict, home: str, node_root: str) -> None:
    pip_index = source_url(manifest["pip_index_url"], "PyPI 索引")
    registry = source_url(manifest["npm_registry"], "npm registry")
    trusted = pip_trusted_host(pip_index)
    env = await environment(worker)
    for key, expected in [
        ("PIP_INDEX_URL", pip_index),
    ]:
        if env.get(key) and env[key].rstrip("/") != expected:
            raise RuntimeError(f"{key} 与固定镜像冲突，已保留原配置，请管理员核对")
    for key in ("PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS"):
        if env.get(key):
            raise RuntimeError(f"{key} 引入额外来源，请管理员核对")
    _check_trusted(env.get("PIP_TRUSTED_HOST", ""), trusted, "PIP_TRUSTED_HOST")
    if env.get("PIP_CONFIG_FILE") == "/dev/null":
        raise RuntimeError("PIP_CONFIG_FILE 禁用了全局配置，请管理员核对")
    pip_paths = _pip_paths(home)
    if env.get("PIP_CONFIG_FILE"):
        pip_paths.append(env["PIP_CONFIG_FILE"])
    pip_found = trust_found = npm_found = False
    for path in dict.fromkeys(pip_paths):
        raw = await worker.ssh.read_file(path)
        if raw is None:
            continue
        config = configparser.ConfigParser(interpolation=None)
        config.read_string(raw.decode())
        for section in [config.default_section, *config.sections()]:
            values = dict(config.items(section))
            if any(values.get(key, "").strip() for key in ("extra-index-url", "find-links")):
                raise RuntimeError(f"pip 配置含额外来源：{path}，未覆盖")
            has_trust = _check_trusted(values.get("trusted-host", ""), trusted, path)
            value = values.get("index-url", "").strip()
            if value and value.rstrip("/") != pip_index:
                raise RuntimeError(f"pip 已有镜像与固定镜像冲突：{path}，未覆盖")
            if section == "global":
                pip_found = pip_found or bool(value)
                trust_found = trust_found or has_trust
    npm_paths = ["/etc/npmrc", node_root + "/etc/npmrc", home + "/.npmrc"]
    for key in (
        "NPM_CONFIG_USERCONFIG",
        "NPM_CONFIG_GLOBALCONFIG",
        "npm_config_userconfig",
        "npm_config_globalconfig",
    ):
        if env.get(key):
            npm_paths.append(env[key])
    for path in dict.fromkeys(npm_paths):
        raw = await worker.ssh.read_file(path)
        if raw is None:
            continue
        for line in raw.decode().splitlines():
            if line.lstrip().startswith(("#", ";")):
                continue
            key, separator, _ = line.strip().partition("=")
            if not separator:
                continue
            if key.strip().lower() == "registry":
                npm_found = True
    # 保留自有 npm 配置；未配置 registry 时才补充部署默认值。
    await worker._ensure_safe()
    if not pip_found or (trusted and not trust_found):
        path = home + "/.config/pip/pip.conf"
        raw = await worker.ssh.read_file(path)
        config = configparser.ConfigParser(interpolation=None)
        config.read_string((raw or b"").decode())
        if not config.has_section("global"):
            config.add_section("global")
        config.set("global", "index-url", pip_index)
        if trusted:
            config.set("global", "trusted-host", trusted)
        output = io.StringIO()
        config.write(output)
        await worker._write_atomic_if_changed(path, output.getvalue().encode(), 0o600, backup=True)
    if not npm_found:
        path = home + "/.npmrc"
        raw = await worker.ssh.read_file(path)
        content = (raw or b"").decode() + f"\nregistry={registry}\n"
        await worker._write_atomic_if_changed(path, content.encode(), 0o600, backup=True)
