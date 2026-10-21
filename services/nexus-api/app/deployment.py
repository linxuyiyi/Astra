"""显式部署步骤；不运行于普通服务请求，也不保存明文密钥。"""

from __future__ import annotations

import argparse
import asyncio
import os
from uuid import uuid4

from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal, validate_database
from app.gateway.models import GatewayKey, utcnow
from app.gateway.security import gateway_key_digest, gateway_key_prefix
from app.gateway.urls import gateway_base
from app.llm_wiki import LlmWikiClient, LlmWikiError


async def register_gateway_key() -> None:
    from nexus_api.model_route import gateway_api_key

    raw = gateway_api_key()
    gateway_base(os.environ.get("NEXUS_GATEWAY_OPENAI_BASE", ""))
    await validate_database()
    async with SessionLocal() as session:
        key = await session.scalar(select(GatewayKey).where(GatewayKey.key_digest == gateway_key_digest(raw)))
        if key:
            if (
                not key.enabled
                or key.revoked_at
                or key.deleted_at
                or (key.expires_at and key.expires_at <= utcnow())
            ):
                raise RuntimeError("平台网关密钥已禁用、删除或过期，请显式提供有效密钥")
            return
        session.add(
            GatewayKey(
                id="gk_" + uuid4().hex,
                name="Nexus platform",
                owner_id="nexus-platform",
                key_prefix=gateway_key_prefix(raw),
                key_digest=gateway_key_digest(raw),
                allowed_models=[],
                enabled=True,
            )
        )
        await session.commit()


async def check_wiki_project() -> None:
    settings = get_settings()
    if not settings.llm_wiki_api_token or not settings.llm_wiki_project_id:
        raise RuntimeError("已有 llm_wiki 需要真实 ASTRA_LLM_WIKI_API_TOKEN 和 ASTRA_LLM_WIKI_PROJECT_ID")
    client = LlmWikiClient(settings)
    await client.health()
    await client.search("Nexus deployment verification", limit=1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Nexus 部署初始化与鉴权预检")
    parser.add_argument("action", choices=["gateway-key", "wiki"])
    action = parser.parse_args().action
    try:
        asyncio.run(register_gateway_key() if action == "gateway-key" else check_wiki_project())
    except (RuntimeError, ValueError, LlmWikiError) as exc:
        # 这些领域诊断不包含 URL 或密钥；其他驱动异常统一隐藏细节。
        parser.exit(1, f"{exc}\n")
    except Exception:
        parser.exit(1, "部署检查失败，请检查数据库或服务连接与权限\n")
    print("平台网关密钥已登记" if action == "gateway-key" else "llm_wiki 项目鉴权与检索通过")


if __name__ == "__main__":
    main()
