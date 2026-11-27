"""网关视觉路由配置、内部识图解析与数据面透明视觉桥接测试。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import vision_routes as vision_routes_module
from app.api.gateway_routes import gateway_error_handler
from app.api.gateway_routes import router as gateway_router
from app.api.vision_routes import clear_vision_cache
from app.api.vision_routes import router as vision_router
from app.database import SessionLocal
from app.gateway.config import RouteConfig
from app.gateway.errors import GatewayDataPlaneError
from app.gateway.models import GatewayAuditEvent, GatewayRequest, GatewayVisionInput
from app.gateway.runtime import SCOPE_KEY, SCOPE_NODE
from app.gateway.security import CredentialCipher
from app.gateway.server import CHAT_PATH, create_gateway_app
from app.gateway.service import GatewayError
from tests.platform.test_gateway_data_plane import (
    DOWNSTREAM_KEY,
    MODEL,
    NODE_A,
    NODE_B,
    POOL_ID,
    auth_headers,
    build_config,
    build_plane,
    node_dict,
)

VISION_MODEL = "vision-helper"
VISION_TARGET = "qwen2.5-vl-7b"
VISION_ROUTE_ID = "gr_vision"
SAMPLE_DATA_URI = (
    "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8U"
    "HRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAA"
    "AAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
)


class DualModalUpstream:
    """区分纯文本主节点与视觉模型节点，校验纯文本节点绝不收到 image_url。"""

    def __init__(self, *, vision_status: int = 200) -> None:
        self.text_requests: list[dict] = []
        self.vision_requests: list[dict] = []
        self.vision_status = vision_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == CHAT_PATH
        assert request.headers["authorization"] == f"Bearer {DOWNSTREAM_KEY}"
        payload = json.loads(request.content)
        if request.url.host == "vision.test":
            self.vision_requests.append(payload)
            if self.vision_status != 200:
                return httpx.Response(self.vision_status, json={"error": {"message": "vision down"}})
            assert payload["model"] == VISION_TARGET
            assert payload.get("stream") is False
            return httpx.Response(
                200,
                json={
                    "id": "cmpl_vision",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": (
                                    "截图显示 Python 报错："
                                    "ModuleNotFoundError: No module named 'demo'"
                                ),
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 25, "total_tokens": 145},
                },
            )

        self.text_requests.append(payload)
        if payload.get("stream"):
            chunk_delta = (
                b'data: {"id":"c1","choices":[{"delta":{"content":"\xe5\xb7\xb2\xe6\x94\xb6'
                b'\xe5\x88\xb0"}}]}\n\n'
            )
            chunk_usage = (
                b'data: {"id":"c1","choices":[{"delta":{}}],'
                b'"usage":{"prompt_tokens":20,"completion_tokens":4,"total_tokens":24}}\n\n'
            )
            chunks = [chunk_delta, chunk_usage, b"data: [DONE]\n\n"]
            return httpx.Response(
                200,
                content=b"".join(chunks),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "id": "cmpl_text",
                "choices": [{"message": {"role": "assistant", "content": "建议安装 demo 依赖。"}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38},
            },
        )


def build_dual_plane(
    upstream: DualModalUpstream,
    *,
    primary_supports_vision: bool = False,
    enable_vision_fallback: bool = True,
):
    cipher = CredentialCipher(Fernet.generate_key().decode())
    config = build_config(
        cipher,
        nodes=[
            node_dict(NODE_A, host="text.test", cipher=cipher, models=(MODEL,)),
            node_dict(NODE_B, host="vision.test", cipher=cipher, models=(VISION_TARGET,)),
        ],
    )
    config.routes[MODEL] = replace(config.routes[MODEL], supports_vision=primary_supports_vision)
    config.routes[VISION_MODEL] = RouteConfig(
        id=VISION_ROUTE_ID,
        model=VISION_MODEL,
        pool_id=POOL_ID,
        enabled=True,
        target_model=VISION_TARGET,
        model_type="CHAT",
        supports_vision=True,
    )
    config = replace(config, vision_route_id=VISION_ROUTE_ID if enable_vision_fallback else None)
    return build_plane(upstream, config=config, cipher=cipher)


def test_data_plane_bridges_image_for_text_only_route_with_cache_and_streaming():
    upstream = DualModalUpstream()
    kv = build_dual_plane(upstream, primary_supports_vision=False, enable_vision_fallback=True)
    with TestClient(create_gateway_app(kv.plane)) as client:
        # 第 1 轮（非流式）：图片发给纯文本路由 -> 自动调用视觉模型解析并替换为文本再发给主模型
        turn1 = client.post(
            CHAT_PATH,
            headers=auth_headers(),
            json={
                "model": MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "这个报错怎么修？"},
                            {"type": "image_url", "image_url": {"url": SAMPLE_DATA_URI}},
                        ],
                    }
                ],
            },
        )
        assert turn1.status_code == 200, turn1.text
        assert turn1.json()["choices"][0]["message"]["content"] == "建议安装 demo 依赖。"
        assert len(upstream.vision_requests) == 1
        assert len(upstream.text_requests) == 1
        sent_turn1 = upstream.text_requests[0]["messages"][0]
        assert isinstance(sent_turn1["content"], str)
        assert "ModuleNotFoundError: No module named 'demo'" in sent_turn1["content"]
        assert "image_url" not in json.dumps(upstream.text_requests[0])

        # 第 2 轮（SSE 流式，历史里仍保留同一张图片）：命中视觉缓存，不再重复调视觉模型
        turn2 = client.post(
            CHAT_PATH,
            headers=auth_headers(),
            json={
                "model": MODEL,
                "stream": True,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "这个报错怎么修？"},
                            {"type": "image_url", "image_url": {"url": SAMPLE_DATA_URI}},
                        ],
                    },
                    {"role": "assistant", "content": "建议安装 demo 依赖。"},
                    {"role": "user", "content": "具体执行什么命令？"},
                ],
            },
        )
        assert turn2.status_code == 200, turn2.text
        assert "已收到" in turn2.text
        assert len(upstream.vision_requests) == 1  # 命中缓存，未再次调用视觉模型
        assert len(upstream.text_requests) == 2
        assert "image_url" not in json.dumps(upstream.text_requests[1])

    assert kv.plane._counters["active"] == 0

    async def check_leases_and_logs():
        assert await kv.store.acquire(SCOPE_KEY, "gk_test", 1, 30)
        assert await kv.store.acquire(SCOPE_NODE, NODE_A, 1, 30)
        assert await kv.store.acquire(SCOPE_NODE, NODE_B, 1, 30)
        async with SessionLocal() as session:
            rows = list(
                await session.scalars(
                    select(GatewayRequest).where(GatewayRequest.model == VISION_MODEL)
                )
            )
            assert rows
            assert rows[-1].actual_model == VISION_TARGET
            assert rows[-1].status_code == 200
            assert rows[-1].total_tokens == 145

    asyncio.run(check_leases_and_logs())


def test_data_plane_passes_images_natively_for_vision_capable_route():
    upstream = DualModalUpstream()
    kv = build_dual_plane(upstream, primary_supports_vision=True, enable_vision_fallback=True)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            CHAT_PATH,
            headers=auth_headers(),
            json={
                "model": MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "看这张图"},
                            {"type": "image_url", "image_url": {"url": SAMPLE_DATA_URI}},
                        ],
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
    assert len(upstream.vision_requests) == 0
    assert len(upstream.text_requests) == 1
    assert isinstance(upstream.text_requests[0]["messages"][0]["content"], list)
    assert upstream.text_requests[0]["messages"][0]["content"][1]["type"] == "image_url"


def test_data_plane_unconfigured_vision_rejects_current_image_but_rescues_historical_images():
    upstream = DualModalUpstream()
    kv = build_dual_plane(upstream, primary_supports_vision=False, enable_vision_fallback=False)
    with TestClient(create_gateway_app(kv.plane)) as client:
        # 当前轮含图且未配置视觉路由 -> 明确返回 400 VISION_NOT_SUPPORTED，不透传给纯文本上游
        current = client.post(
            CHAT_PATH,
            headers=auth_headers(),
            json={
                "model": MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "帮我看图"},
                            {"type": "image_url", "image_url": {"url": SAMPLE_DATA_URI}},
                        ],
                    }
                ],
            },
        )
        assert current.status_code == 400
        assert current.json()["error"]["code"] == "VISION_NOT_SUPPORTED"
        assert len(upstream.text_requests) == 0

        # 仅旧历史含图（例如员工从视觉模型切回纯文本模型后继续纯文本聊天）-> 省略旧图避免整条会话锁死
        history_only = client.post(
            CHAT_PATH,
            headers=auth_headers(),
            json={
                "model": MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "之前的截图"},
                            {"type": "image_url", "image_url": {"url": SAMPLE_DATA_URI}},
                        ],
                    },
                    {"role": "assistant", "content": "好的，看到了。"},
                    {"role": "user", "content": "那我们继续讨论接口设计吧"},
                ],
            },
        )
        assert history_only.status_code == 200, history_only.text
        assert len(upstream.text_requests) == 1
        assert "历史图片已省略" in upstream.text_requests[0]["messages"][0]["content"]
        assert "image_url" not in json.dumps(upstream.text_requests[0])
    assert kv.plane._counters["active"] == 0


def test_data_plane_vision_bridge_failure_releases_leases():
    upstream = DualModalUpstream(vision_status=503)
    kv = build_dual_plane(upstream, primary_supports_vision=False, enable_vision_fallback=True)
    with TestClient(create_gateway_app(kv.plane)) as client:
        response = client.post(
            CHAT_PATH,
            headers=auth_headers(),
            json={
                "model": MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image_url", "image_url": {"url": SAMPLE_DATA_URI}}],
                    }
                ],
            },
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "VISION_BRIDGE_FAILED"
    assert len(upstream.text_requests) == 0
    assert kv.plane._counters["active"] == 0


def test_persisted_vision_config_audit_and_internal_analyze_with_cache(monkeypatch):
    clear_vision_cache()

    def fake_require_user(request):
        uid = request.headers.get("X-Verified-UID")
        if not uid:
            raise HTTPException(401, "请先登录")
        return {"uid": uid}

    monkeypatch.setattr(vision_routes_module, "require_user", fake_require_user)
    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "internal-vision-secret")
    monkeypatch.setenv("NEXUS_GATEWAY_OPENAI_BASE", "http://gateway.test/v1")
    monkeypatch.setenv("NEXUS_GATEWAY_API_KEY", "server-only-vision-key")

    app = FastAPI()
    app.include_router(gateway_router)
    app.include_router(vision_router)
    app.add_exception_handler(GatewayError, gateway_error_handler)
    app.add_exception_handler(GatewayDataPlaneError, lambda _r, exc: exc.to_response())

    seen_gateway_calls: list[dict] = []
    original_client = httpx.AsyncClient

    def fake_gateway(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://gateway.test/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer server-only-vision-key"
        body = json.loads(request.content)
        seen_gateway_calls.append(body)
        prompt_text = body["messages"][1]["content"][0]["text"]
        answer = "聚焦堆栈：ZeroDivisionError" if "堆栈" in prompt_text else "整图摘要：一张控制台截图"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": answer}}]},
        )

    monkeypatch.setattr(
        "app.api.vision_routes.httpx.AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(fake_gateway), **kw),
    )

    user_headers = {"X-Verified-UID": "vision-qa"}
    internal_headers = {"X-Nexus-Internal-Key": "internal-vision-secret"}
    image_b64 = SAMPLE_DATA_URI.split(",", 1)[1]

    with TestClient(app) as client:
        # 鉴权校验
        assert client.put("/api/gateway/vision-input", json={"route_id": None}).status_code == 401
        assert (
            client.post("/api/v1/internal/vision/analyze", json={"image_base64": image_b64}).status_code
            == 401
        )
        assert (
            client.put("/api/gateway/vision-input", headers=user_headers, json={"route_id": None}).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/internal/vision/analyze",
                headers=internal_headers,
                json={"image_base64": image_b64},
            ).status_code
            == 422
        )

        pool = client.post("/api/gateway/pools", json={"name": f"vision-{uuid4().hex}"}).json()
        # 非 CHAT 路由不允许开启 supports_vision
        bad_asr = client.post(
            "/api/gateway/routes",
            json={
                "model": f"asr-{uuid4().hex[:6]}",
                "model_type": "ASR",
                "supports_vision": True,
                "pool_id": pool["id"],
            },
        )
        assert bad_asr.status_code == 409

        text_route = client.post(
            "/api/gateway/routes",
            json={
                "model": f"deepseek-{uuid4().hex[:6]}",
                "model_type": "CHAT",
                "supports_vision": False,
                "pool_id": pool["id"],
            },
        ).json()
        # 不支持视觉的 CHAT 路由不能设为默认视觉解析路由
        assert (
            client.put(
                "/api/gateway/vision-input",
                headers=user_headers,
                json={"route_id": text_route["id"]},
            ).status_code
            == 422
        )

        vlm_route = client.post(
            "/api/gateway/routes",
            json={
                "model": f"qwen-vl-{uuid4().hex[:6]}",
                "model_type": "CHAT",
                "supports_vision": True,
                "pool_id": pool["id"],
            },
        ).json()
        assert vlm_route["supports_vision"] is True
        assert (
            client.put(
                "/api/gateway/vision-input",
                headers=user_headers,
                json={"route_id": vlm_route["id"]},
            ).status_code
            == 200
        )
        assert client.get("/api/gateway/vision-input").json() == {"route_id": vlm_route["id"]}

        # 首次调用通用解析
        res1 = client.post(
            "/api/v1/internal/vision/analyze",
            headers=internal_headers,
            json={"image_base64": image_b64, "filename": "err.png", "sha256": "abc123"},
        )
        assert res1.status_code == 200, res1.text
        assert res1.json() == {
            "text": "整图摘要：一张控制台截图",
            "model": vlm_route["model"],
            "cached": False,
        }
        assert "server-only-vision-key" not in res1.text

        # 同图同问题命中缓存
        res2 = client.post(
            "/api/v1/internal/vision/analyze",
            headers=internal_headers,
            json={"image_base64": image_b64, "filename": "err.png", "sha256": "abc123"},
        )
        assert res2.status_code == 200
        assert res2.json()["cached"] is True
        assert len(seen_gateway_calls) == 1

        # 带新 question 追问时重新调用视觉模型
        res3 = client.post(
            "/api/v1/internal/vision/analyze",
            headers=internal_headers,
            json={
                "image_base64": image_b64,
                "filename": "err.png",
                "sha256": "abc123",
                "question": "请提取红色报错堆栈",
            },
        )
        assert res3.status_code == 200
        assert res3.json() == {
            "text": "聚焦堆栈：ZeroDivisionError",
            "model": vlm_route["model"],
            "cached": False,
        }
        assert len(seen_gateway_calls) == 2

        # 删除该路由自动清空默认视觉解析配置
        assert client.delete(f"/api/gateway/routes/{vlm_route['id']}").status_code == 204
        assert client.get("/api/gateway/vision-input").json() == {"route_id": None}

    async def check_audit():
        async with SessionLocal() as session:
            rows = list(
                await session.scalars(
                    select(GatewayAuditEvent).where(GatewayAuditEvent.action == "vision-input.update")
                )
            )
            assert rows and rows[-1].actor == "vision-qa"
            assert (await session.get(GatewayVisionInput, "default")).route_id is None

    asyncio.run(check_audit())


def test_internal_agent_vision_capability_endpoint(tmp_path, monkeypatch):
    from sqlalchemy import text

    from app.config.settings import get_settings
    from app.sql_storage import connect, create_test_schema
    from nexus_api.main import create_app

    db_path = tmp_path / "vision-cap.db"
    create_test_schema(db_path)
    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "internal-cap-key")
    monkeypatch.setenv("ASTRA_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    get_settings.cache_clear()

    try:
        with connect(db_path) as conn:
            conn.execute(
                text(
                    "INSERT INTO t_nexus_gateway_pools "
                    "(id, name, description, enabled, sort_order, created_at, updated_at) "
                    "VALUES ('gp_cap', 'cap', '', 1, 0, '2026-11-27 00:00:00', '2026-11-27 00:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO t_nexus_gateway_routes "
                    "(id, model, model_type, supports_vision, pool_id, target_model, "
                    "description, enabled, created_at, updated_at) "
                    "VALUES "
                    "('gr_text', 'deepseek-v3', 'CHAT', 0, 'gp_cap', NULL, '', 1, "
                    "'2026-11-27 00:00:00', '2026-11-27 00:00:00'),"
                    "('gr_vlm', 'qwen-vl', 'CHAT', 1, 'gp_cap', NULL, '', 1, "
                    "'2026-11-27 00:00:00', '2026-11-27 00:00:00')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO t_nexus_employees "
                    "(id, name, domain, description, instructions, avatar, enabled, "
                    "knowledge_json, skills_json, mcp_json, tools_json, agentscope_agent_id, "
                    "mailbox_session_id, capability_json, model_route_id, created_at, updated_at) "
                    "VALUES "
                    "('emp_text', '纯文本员工', '研发', '', '', '', 1, '[]', '[]', '[]', '[]', "
                    "'as_text', NULL, '{}', 'gr_text', '2026-11-27', '2026-11-27'),"
                    "('emp_vlm', '多模态员工', '设计', '', '', '', 1, '[]', '[]', '[]', '[]', "
                    "'as_vlm', NULL, '{}', 'gr_vlm', '2026-11-27', '2026-11-27')"
                )
            )
            conn.commit()

        app = create_app()
        headers = {"X-Nexus-Internal-Key": "internal-cap-key"}
        with TestClient(app) as client:
            unauth = client.get(
                "/api/v1/internal/agent-vision-capability", params={"agent_id": "as_text"}
            )
            assert unauth.status_code == 401
            res_text = client.get(
                "/api/v1/internal/agent-vision-capability", headers=headers, params={"agent_id": "as_text"}
            )
            assert res_text.status_code == 200
            assert res_text.json() == {
                "supports_vision": False,
                "route_model": "deepseek-v3",
                "vision_fallback_configured": False,
            }

            res_vlm = client.get(
                "/api/v1/internal/agent-vision-capability", headers=headers, params={"agent_id": "as_vlm"}
            )
            assert res_vlm.status_code == 200
            assert res_vlm.json() == {
                "supports_vision": True,
                "route_model": "qwen-vl",
                "vision_fallback_configured": False,
            }

            with connect(db_path) as conn:
                conn.execute(
                    text(
                        "INSERT INTO t_nexus_gateway_vision_input (id, route_id) "
                        "VALUES ('default', 'gr_vlm')"
                    )
                )
                conn.commit()

            res_after = client.get(
                "/api/v1/internal/agent-vision-capability", headers=headers, params={"agent_id": "as_text"}
            )
            assert res_after.status_code == 200
            assert res_after.json()["vision_fallback_configured"] is True
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
