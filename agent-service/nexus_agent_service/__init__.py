# -*- coding: utf-8 -*-
"""Nexus 独立 AgentScope Agent Service 适配层。

正式执行能力来自官方 ``agentscope`` 2.0.10 包；本包只做配置、装配与
只读扩展，不复制或修改官方核心源码。
"""
from .app import build_app
from .bootstrap import ModelBootstrapStatus, bootstrap_default_model
from .config import ServiceConfig, load_config

__all__ = [
    "build_app",
    "load_config",
    "ServiceConfig",
    "bootstrap_default_model",
    "ModelBootstrapStatus",
]
