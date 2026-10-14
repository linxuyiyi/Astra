# -*- coding: utf-8 -*-
"""默认模型凭据引导。

使用官方公开的 ``CredentialBase`` 子类与 ``StorageBase`` API 在服务
lifespan 内写入真实凭据；固定 credential id 使重复启动命中同一条记录，
不会每次重启新建重复凭据。密钥只从服务配置读取，绝不返回或记录。
"""
from __future__ import annotations

from dataclasses import dataclass

from agentscope.app.storage import StorageBase
from agentscope.credential import DeepSeekCredential, OllamaCredential

from .config import (
    DEFAULT_CREDENTIAL_TYPE,
    EMBEDDING_CREDENTIAL_TYPE,
    ServiceConfig,
)

MODEL_NOT_CONFIGURED_REASON = (
    "未配置 AGENTSCOPE_MODEL_API_KEY，模型推理不可用；服务本身仍可提供"
    "原生 Agent/Session CRUD。请在服务环境或根 .env 中配置后重启。"
)

# 知识库启用但 embedding 凭据未引导时，如实不可用；不伪装 RAG 已就绪。
EMBEDDING_NOT_CONFIGURED_REASON = (
    "知识库已启用但 embedding 凭据未引导，检索不可用。"
)

# 引导失败的统一对外原因：绝不携带原始异常、SQL 参数或密钥。
BOOTSTRAP_ERROR_REASON = (
    "默认模型凭据引导失败：内部存储读写异常，请检查数据目录与内部租户配置。"
)


@dataclass(frozen=True)
class ModelBootstrapStatus:
    """默认模型引导结果，供健康检查与扩展接口读取。"""

    configured: bool
    credential_id: str | None
    model_name: str | None
    reason: str | None

    def to_public_dict(self) -> dict:
        """脱敏字典，绝不含密钥。"""
        return {
            "model_configured": self.configured,
            "model_name": self.model_name,
            "credential_id": self.credential_id,
            "reason": self.reason,
        }


async def bootstrap_default_model(
    storage: StorageBase,
    config: ServiceConfig,
) -> ModelBootstrapStatus:
    """确保内部租户下存在默认 DeepSeek 凭据。

    * 有密钥：按固定 id 写入（或更新）一条 ``deepseek_credential``；
    * 无密钥但原生存储已持久化同 id 的有效凭据：复用该持久凭据，仍视为
      已配置，避免每次重启都要求重新提交已落库的密钥；
    * 新装、无密钥且无持久凭据：返回明确的未配置状态，不伪造可用模型。

    任何底层存储异常都在此边界收敛为不含原始异常/参数/密钥的
    ``RuntimeError``，明确失败而不是伪装健康。
    """
    try:
        return await _bootstrap_default_model(storage, config)
    except Exception:  # noqa: BLE001 - 边界统一脱敏，不泄露底层异常
        raise RuntimeError(BOOTSTRAP_ERROR_REASON) from None


async def _bootstrap_default_model(
    storage: StorageBase,
    config: ServiceConfig,
) -> ModelBootstrapStatus:
    if config.model_configured:
        credential = DeepSeekCredential(
            id=config.credential_id,
            name=config.credential_name,
            api_key=config.model_api_key,
            base_url=config.model_base_url,
        )
        credential_id = await storage.upsert_credential(
            config.internal_user_id,
            credential,
        )
        return ModelBootstrapStatus(
            configured=True,
            credential_id=credential_id,
            model_name=config.model_name,
            reason=None,
        )

    # 无 env 密钥：尝试复用原生存储中已持久化的内部凭据。
    existing = await storage.get_credential(
        config.internal_user_id,
        config.credential_id,
    )
    if _is_valid_deepseek_credential(existing):
        return ModelBootstrapStatus(
            configured=True,
            credential_id=existing.id,
            model_name=config.model_name,
            reason=None,
        )

    return ModelBootstrapStatus(
        configured=False,
        credential_id=None,
        model_name=config.model_name,
        reason=MODEL_NOT_CONFIGURED_REASON,
    )


def _is_valid_deepseek_credential(record: object) -> bool:
    """判断记录是否为该内部租户下有效的原生 DeepSeek 凭据。"""
    data = getattr(record, "data", None)
    if not isinstance(data, dict):
        return False
    if data.get("type") != DEFAULT_CREDENTIAL_TYPE:
        return False
    return bool(data.get("api_key"))


@dataclass(frozen=True)
class EmbeddingBootstrapStatus:
    """知识库 embedding 凭据引导结果（脱敏，绝不含密钥/主机以外的秘密）。"""

    enabled: bool
    configured: bool
    credential_id: str | None
    model_name: str | None
    dimensions: int | None
    reason: str | None

    def to_public_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "credential_id": self.credential_id,
            "model_name": self.model_name,
            "dimensions": self.dimensions,
            "reason": self.reason,
        }


async def bootstrap_embedding_credential(
    storage: StorageBase,
    config: ServiceConfig,
) -> EmbeddingBootstrapStatus:
    """知识库启用时按固定 id 引导原生 OllamaCredential。

    只写入连接信息（``id`` 固定、``name``、``host``），使重复启动命中同一条
    记录（upsert 幂等、重启复用），不在 Nexus 保存模型配置。知识库未启用时
    返回未启用状态，不写任何记录。宿主地址来自服务环境配置。
    """
    if not config.knowledge_enabled:
        return EmbeddingBootstrapStatus(
            enabled=False,
            configured=False,
            credential_id=None,
            model_name=config.embedding_model,
            dimensions=config.embedding_dimensions,
            reason=None,
        )
    try:
        credential = OllamaCredential(
            id=config.embedding_credential_id,
            name=config.embedding_credential_name,
            host=config.embedding_host,
        )
        credential_id = await storage.upsert_credential(
            config.internal_user_id,
            credential,
        )
    except Exception:  # noqa: BLE001 - 边界统一脱敏
        raise RuntimeError(BOOTSTRAP_ERROR_REASON) from None
    return EmbeddingBootstrapStatus(
        enabled=True,
        configured=True,
        credential_id=credential_id,
        model_name=config.embedding_model,
        dimensions=config.embedding_dimensions,
        reason=None,
    )


def is_valid_ollama_credential(record: object) -> bool:
    """判断记录是否为该内部租户下有效的原生 Ollama 凭据（诊断用）。"""
    data = getattr(record, "data", None)
    if not isinstance(data, dict):
        return False
    return data.get("type") == EMBEDDING_CREDENTIAL_TYPE
