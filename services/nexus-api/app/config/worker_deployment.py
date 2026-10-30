"""Worker 安装清单：来源分别配置，远端只消费解析后的清单。"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

PYTHON_VERSION = "3.11.17"
PYTHON_SHA256 = "bfb74ad39efae27cda510f134ab408e00f9992c56851cfc0b1cdb5646da11599"
DEFAULT_PYTHON_SOURCE = (
    f"https://mirrors.tools.huawei.com/python/{PYTHON_VERSION}/Python-{PYTHON_VERSION}.tar.xz"
)
DEFAULT_NODE_BASE = "https://mirrors.tools.huawei.com/nodejs"
DEFAULT_NPM_REGISTRY = "https://mirrors.tools.huawei.com/npm/"
DEFAULT_PIP_INDEX = "https://mirrors.tools.huawei.com/pypi/simple"
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")


def source_url(value: str, name: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in value)
    ):
        raise ValueError(f"{name} 必须是无凭据、查询参数和空白的 HTTP(S) 地址")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} 端口无效") from exc
    return value.rstrip("/")


def deployment_manifest(settings) -> dict:
    if settings.remote_download_base_url:
        raise ValueError(
            "NEXUS_REMOTE_DOWNLOAD_BASE_URL 已停用；请分别配置 NEXUS_REMOTE_PYTHON_SOURCE_URL、"
            "NEXUS_REMOTE_NODE_BASE_URL、NEXUS_REMOTE_NPM_REGISTRY 和 NEXUS_REMOTE_PIP_INDEX_URL，"
            "系统软件仓库沿用目标机配置"
        )
    for name in ("remote_node_version", "remote_opencode_version"):
        if not _VERSION.fullmatch(getattr(settings, name)):
            raise ValueError(f"{name} 必须为固定语义版本，不能使用 latest 或 URL")
    node = settings.remote_node_binary
    if node and (not node.startswith("/") or not node.endswith("/node") or any(c.isspace() for c in node)):
        raise ValueError("NEXUS_REMOTE_NODE_BINARY 必须是名为 node 的绝对可执行文件路径")
    hosts = [host.strip().lower() for host in settings.remote_npm_artifact_hosts.split(",") if host.strip()]
    for host in hosts:
        parsed = urlsplit("https://" + host)
        if parsed.netloc != host or not parsed.hostname or parsed.path or parsed.username or parsed.password:
            raise ValueError("NEXUS_REMOTE_NPM_ARTIFACT_HOSTS 只接受逗号分隔的主机名及可选端口")
    return {
        "python_version": PYTHON_VERSION,
        "python_sha256": PYTHON_SHA256,
        "python_source_url": source_url(settings.remote_python_source_url, "Python 源码 URL"),
        "node_base_url": source_url(settings.remote_node_base_url, "Node.js 分发 URL"),
        "node_version": settings.remote_node_version,
        "node_binary": settings.remote_node_binary,
        "opencode_version": settings.remote_opencode_version,
        "npm_registry": source_url(settings.remote_npm_registry, "npm registry"),
        "npm_artifact_hosts": hosts,
        "pip_index_url": source_url(settings.remote_pip_index_url, "PyPI 索引"),
    }
