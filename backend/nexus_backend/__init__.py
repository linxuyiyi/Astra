# -*- coding: utf-8 -*-
"""Nexus 业务后端包。

模块化单体：只通过公开 HTTP 接口集成独立部署的 AgentScope 服务，不导入
其私有模块，也不保存模型密钥或第二套 Agent/Session/Memory。
"""
from .adapter import AgentScopeHTTPClient, UpstreamError, UpstreamProbe
from .app import build_app
from .config import BackendConfig, load_config

__all__ = [
    "build_app",
    "load_config",
    "BackendConfig",
    "AgentScopeHTTPClient",
    "UpstreamError",
    "UpstreamProbe",
]
