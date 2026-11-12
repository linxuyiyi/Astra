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
        app = create_app(storage=storage, workspace_manager=manager, message_bus=InMemoryMessageBus())
        register_workspace_attachments(app, storage)
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
                assert (await client.get(path, params=location)).status_code == 404
                assert (await client.get(path, params=group, headers={"X-User-ID": "other"})).status_code == 404
                assert (await client.get(path, params={**group, "agent_id": "employee"})).status_code == 404

                params = {**group, "target_agent_id": "employee", "target_session_id": a.id}
                assert (await client.post(path + "/copy", params=params)).status_code == 200
                assert (await client.post(path + "/copy", params=params)).json() == {"copied": True}
                assert (await client.get(path, params=other)).status_code == 404
                data = (await client.get(path, params=location)).content
                assert hashlib.sha256(data).hexdigest() == expected
                assert (await client.put(f"{prefix}/att_{'b' * 32}", params=group, content=b"")).status_code == 400
                assert (await client.put(
                    f"{prefix}/att_{'b' * 32}", params=group, content=binary + b"!",
                )).status_code == 413
                assert (await client.get(f"{prefix}/att_{'b' * 32}", params=group)).status_code == 404
                assert not list(original.parent.glob(".upload-*"))

                employee_workspace = await app.state.workspace_service.resolve("nexus", **location)
                assert employee_workspace.workdir != group_workspace.workdir
                directory = Path(employee_workspace.workdir) / "attachments"
                symlink_id = "att_" + "e" * 32
                (directory / symlink_id).symlink_to(original)
                assert (await client.get(f"{prefix}/{symlink_id}", params=location)).status_code == 404

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
