import asyncio
import base64
from io import BytesIO
from pathlib import Path

from agentscope.message import DataBlock
from agentscope.tool import FunctionTool
from chat_attachments import _render
from PIL import Image


def test_image_attachment_tool_returns_bounded_multimodal_data():
    original = BytesIO()
    Image.new("RGB", (3840, 2160), "red").save(original, "PNG")

    def preview():
        return _render(original.getvalue(), "screenshot.png", "image/png", 0, 1)

    chunk = asyncio.run(FunctionTool(preview).call())
    (image,) = chunk.content
    assert isinstance(image, DataBlock)
    assert image.source.media_type == "image/jpeg"
    raw = base64.b64decode(image.source.data)
    assert len(raw) <= 512 * 1024
    with Image.open(BytesIO(raw)) as decoded:
        assert decoded.size == (1920, 1080)


def test_workspace_attachments_real_storage(tmp_path, monkeypatch):
    import hashlib

    import httpx
    from agentscope.app import create_app
    from agentscope.app.message_bus import InMemoryMessageBus
    from agentscope.app.storage import AsyncSQLAlchemyStorage, SessionConfig
    from agentscope.app.workspace_manager import IsolationPolicy
    from group_security import AgentServiceKeyMiddleware
    from schedule_workspace import ScheduleWorkspaceManager
    from workspace_attachments import MAX_BYTES, register_workspace_attachments

    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "test-service-only")

    async def scenario():
        storage = AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}")
        manager = ScheduleWorkspaceManager(str(tmp_path / "workspace"), isolation=IsolationPolicy.PER_SESSION)
        bus = InMemoryMessageBus()
        app = create_app(storage=storage, workspace_manager=manager, message_bus=bus)
        register_workspace_attachments(app, storage, bus)
        app.add_middleware(AgentServiceKeyMiddleware)
        async with app.router.lifespan_context(app):
            channel_id = "ch_" + "3" * 12
            a = await storage.upsert_session(
                "nexus", "employee", SessionConfig(workspace_id=f"ws-{channel_id}-employee"),
            )
            b = await storage.upsert_session("nexus", "employee", SessionConfig(workspace_id="chat-b"))
            transport = httpx.ASGITransport(app=app)
            headers = {"X-User-ID": "nexus", "X-Nexus-Internal-Key": "test-service-only"}
            async with httpx.AsyncClient(transport=transport, base_url="http://agent", headers=headers) as client:
                prefix = "/nexus/workspace-attachments"
                group_path = f"{prefix}/groups/{channel_id}"
                assert (await client.put(group_path, headers={"X-Nexus-Internal-Key": ""})).status_code == 401
                group = (await client.put(group_path)).json()
                assert (await client.put(group_path)).json() == group
                location = {"agent_id": "employee", "session_id": a.id}
                other = {"agent_id": "employee", "session_id": b.id}
                path = f"{prefix}/att_{'a' * 32}"
                binary = bytes(range(256)) * (MAX_BYTES // 256)
                expected = hashlib.sha256(binary).hexdigest()
                result = await client.put(path, params=group, content=binary)
                assert result.status_code == 200, result.text
                assert result.json()["sha256"] == expected
                group_workspace = await app.state.workspace_service.resolve("nexus", **group)
                original = Path(group_workspace.workdir) / result.json()["path"]
                with original.open("rb") as file:
                    assert hashlib.file_digest(file, "sha256").hexdigest() == expected
                assert (await client.put(path, params=group, content=b"changed")).status_code == 409
                assert (await client.put(path, params=group, content=binary)).status_code == 200
                assert (await client.get(path, params=location)).status_code == 410
                assert (await client.get(path, params=group, headers={"X-User-ID": "other"})).status_code == 404
                assert (await client.get(path, params={**group, "agent_id": "employee"})).status_code == 404

                params = {**group, "target_agent_id": "employee", "target_session_id": a.id}
                assert (await client.post(path + "/copy", params=params)).status_code == 200
                assert (await client.post(path + "/copy", params=params)).json() == {"copied": True}
                assert (await client.get(path, params=other)).status_code == 410
                check = {**location, "workspace_id": f"ws-{channel_id}-employee", "keys": [path.rsplit("/", 1)[-1]]}
                assert (await client.post(f"{prefix}/verify", json=check)).json() == {"missing": []}
                assert (await client.post(f"{prefix}/verify", json={**check, "workspace_id": "chat-b"})).status_code == 409
                missing = "att_" + "f" * 32
                assert (await client.post(f"{prefix}/{missing}/copy", params=params)).status_code == 410
                assert (await client.post(f"{prefix}/verify", json={**check, "keys": [missing]})).json() == {
                    "missing": [missing],
                }
                data = (await client.get(path, params=location)).content
                assert hashlib.sha256(data).hexdigest() == expected
                assert (await client.put(f"{prefix}/att_{'b' * 32}", params=group, content=b"")).status_code == 400
                assert (await client.put(
                    f"{prefix}/att_{'b' * 32}", params=group, content=binary + b"!",
                )).status_code == 413
                assert (await client.get(f"{prefix}/att_{'b' * 32}", params=group)).status_code == 410
                assert not list(original.parent.glob(".upload-*"))

                employee_workspace = await app.state.workspace_service.resolve("nexus", **location)
                assert employee_workspace.workdir != group_workspace.workdir
                directory = Path(employee_workspace.workdir) / "attachments"
                symlink_id = "att_" + "e" * 32
                (directory / symlink_id).symlink_to(original)
                assert (await client.get(f"{prefix}/{symlink_id}", params=location)).status_code == 410

                retire = f"{prefix}/group-member"
                scope = {"channel_id": channel_id, "employee_id": "employee"}
                assert (await client.delete(retire, params={**other, **scope})).status_code == 409
                assert (await client.delete(retire, params={**location, **scope})).status_code == 204
                assert (await client.delete(retire, params={**location, **scope})).status_code == 204
                assert not (directory / original.name).exists()
                assert not (directory / symlink_id).is_symlink()
                assert await storage.get_session("nexus", "employee", a.id) is None
                assert (await client.get(path, params=group)).status_code == 200

    asyncio.run(scenario())


def test_workspace_upload_rejects_directory_swap_and_cleans_partial_file(tmp_path):
    from fastapi import HTTPException
    from workspace_attachments import save

    workdir = tmp_path / "workspace"
    workdir.mkdir()
    outside = tmp_path / "other-workspace"
    outside.mkdir()
    directory = workdir / "attachments"
    detached = workdir / "displaced"
    key = "att_" + "4" * 32

    async def chunks():
        yield b"first"
        directory.rename(detached)
        directory.symlink_to(outside, target_is_directory=True)
        yield b"second"

    async def scenario():
        try:
            await save(workdir, key, chunks())
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("上传过程中工作区附件目录被替换，应该拒绝发布")
        assert list(detached.iterdir()) == []
        assert list(outside.iterdir()) == []

    asyncio.run(scenario())


def test_native_file_tools_are_bound_to_the_current_workspace(tmp_path):
    import json

    import pytest
    from agentscope.message import ToolCallBlock
    from group_security import workspace_file_call

    root = tmp_path / "chat-a"
    root.mkdir()
    other = tmp_path / "chat-b"
    other.mkdir()
    outside = other / "report.txt"
    outside.write_text("private")
    (root / "borrowed").symlink_to(outside)

    def read(path):
        return ToolCallBlock(id="read", name="Read", input=json.dumps({"file_path": str(path)}))

    for path in (outside, "../chat-b/report.txt", "borrowed"):
        with pytest.raises(PermissionError):
            workspace_file_call(read(path), root)
    scoped = workspace_file_call(read("output.txt"), root)
    assert json.loads(scoped.input)["file_path"] == str(root / "output.txt")


def test_read_attachment_routes_images_by_employee_vision_capability(tmp_path, monkeypatch):
    import json

    import chat_attachments
    import httpx
    from agentscope.app import create_app
    from agentscope.app.message_bus import InMemoryMessageBus
    from agentscope.app.storage import AsyncSQLAlchemyStorage, SessionConfig
    from agentscope.app.workspace_manager import IsolationPolicy
    from agentscope.message import Msg, TextBlock
    from schedule_workspace import ScheduleWorkspaceManager

    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "test-vision-key")
    original_client = httpx.AsyncClient
    state = {"supports_vision": False, "fallback_configured": True, "fail_cap": False}
    analyze_calls: list[dict] = []

    def nexus_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-nexus-internal-key"] == "test-vision-key"
        if request.url.path == "/api/v1/internal/agent-vision-capability":
            if state["fail_cap"]:
                return httpx.Response(503, json={"detail": "down"})
            return httpx.Response(
                200,
                json={
                    "supports_vision": state["supports_vision"],
                    "route_model": "qwen-vl" if state["supports_vision"] else "deepseek-chat",
                    "vision_fallback_configured": state["fallback_configured"],
                },
            )
        if request.url.path == "/api/v1/internal/vision/analyze":
            body = json.loads(request.content)
            analyze_calls.append(body)
            return httpx.Response(
                200,
                json={
                    "text": f"识别结果（问题={body.get('question') or '默认'}）：报错行 L42",
                    "model": "qwen-vl-mini",
                    "cached": False,
                },
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        chat_attachments.httpx,
        "AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(nexus_handler), **kw),
    )

    async def scenario():
        storage = AsyncSQLAlchemyStorage(f"sqlite+aiosqlite:///{tmp_path / 'agent-vision.db'}")
        manager = ScheduleWorkspaceManager(str(tmp_path / "ws"), isolation=IsolationPolicy.PER_SESSION)
        bus = InMemoryMessageBus()
        app = create_app(storage=storage, workspace_manager=manager, message_bus=bus)
        async with app.router.lifespan_context(app):
            session = await storage.upsert_session("nexus", "agent-1", SessionConfig(workspace_id="ws-1"))
            att_id = "att_" + "1" * 32
            msg = Msg(
                id="msg_img_1",
                name="user",
                role="user",
                content=[TextBlock(text="看看这张报错截图")],
                metadata={
                    "attachments": [
                        {"id": att_id, "name": "error.png", "type": "image/png", "size": 123},
                    ]
                },
            )
            await storage.upsert_message("nexus", session.id, msg)
            ws = await app.state.workspace_service.resolve("nexus", "agent-1", session.id)
            att_dir = Path(ws.workdir) / "attachments"
            att_dir.mkdir(parents=True, exist_ok=True)
            img_buf = BytesIO()
            Image.new("RGB", (640, 480), "blue").save(img_buf, "PNG")
            (att_dir / att_id).write_bytes(img_buf.getvalue())

            # 1) 纯文本员工 + 已配平台视觉路由 -> 返回纯文本解析，不返回 DataBlock，并透传 question
            chunk_text = await chat_attachments.read_attachment(
                storage, app.state.workspace_service, "http://nexus.test",
                "nexus", "agent-1", session.id, "msg_img_1", att_id, 0, 1,
                question="提取红色报错行号",
            )
            (block1,) = chunk_text.content
            assert isinstance(block1, TextBlock)
            assert "qwen-vl-mini" in block1.text
            assert "报错行 L42" in block1.text
            assert len(analyze_calls) == 1
            assert analyze_calls[0]["question"] == "提取红色报错行号"
            assert analyze_calls[0]["filename"] == "error.png"

            # 2) 原生多模态员工 -> 直接返回 DataBlock，不额外调视觉解析接口
            state["supports_vision"] = True
            chunk_native = await chat_attachments.read_attachment(
                storage, app.state.workspace_service, "http://nexus.test",
                "nexus", "agent-1", session.id, "msg_img_1", att_id, 0, 1,
            )
            (block2,) = chunk_native.content
            assert isinstance(block2, DataBlock)
            assert block2.source.media_type == "image/jpeg"
            assert len(analyze_calls) == 1

            # 3) 纯文本员工且平台未配置视觉路由 -> 返回友好提示 TextBlock，不污染历史
            state["supports_vision"] = False
            state["fallback_configured"] = False
            chunk_unconfigured = await chat_attachments.read_attachment(
                storage, app.state.workspace_service, "http://nexus.test",
                "nexus", "agent-1", session.id, "msg_img_1", att_id, 0, 1,
            )
            (block3,) = chunk_unconfigured.content
            assert isinstance(block3, TextBlock)
            assert "默认图像理解路由" in block3.text

            # 4) 能力查询失败 -> 安全降级为 TextBlock，不把 DataBlock 放进历史
            state["fail_cap"] = True
            chunk_failed = await chat_attachments.read_attachment(
                storage, app.state.workspace_service, "http://nexus.test",
                "nexus", "agent-1", session.id, "msg_img_1", att_id, 0, 1,
            )
            (block4,) = chunk_failed.content
            assert isinstance(block4, TextBlock)
            assert "无法确认当前模型的图像识别能力" in block4.text

    asyncio.run(scenario())
