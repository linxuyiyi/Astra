"""受信 Nexus 请求使用的 Session Workspace 文件入口。"""

import asyncio
import errno
import hashlib
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from agentscope.app.deps import get_current_user_id
from agentscope.app.storage import SessionConfig
from fastapi import Depends, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

MAX_BYTES = 20 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
ATTACHMENT_ID = r"^att_[0-9a-f]{32}$"
GROUP_ID = r"^ch_[0-9a-f]{12,32}$"
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def attachment_directory(workdir, *, create=False):
    # 逐级打开目录，文件操作始终相对于已打开的目录描述符。
    fd = os.open("/", DIR_FLAGS)
    try:
        for name in Path(workdir).absolute().parts[1:]:
            child = os.open(name, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        if create:
            try:
                os.mkdir("attachments", mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
        child = os.open("attachments", DIR_FLAGS, dir_fd=fd)
        try:
            yield child
        finally:
            os.close(child)
    finally:
        os.close(fd)


def open_file(directory, key):
    fd = os.open(key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= MAX_BYTES:
        os.close(fd)
        raise HTTPException(404, "附件不可用")
    return os.fdopen(fd, "rb")


def open_attachment(workdir, key):
    try:
        with attachment_directory(workdir) as directory:
            return open_file(directory, key)
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            raise
        raise HTTPException(404, "附件不可用") from exc


def media_type(head):
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    return "application/octet-stream"


async def save(workdir, key, chunks):
    size = 0
    digest = hashlib.sha256()
    head = b""
    temporary = f".upload-{uuid4().hex}"
    try:
        with attachment_directory(workdir, create=True) as directory:
            fd = os.open(
                temporary, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o400, dir_fd=directory,
            )
            try:
                with os.fdopen(fd, "wb") as file:
                    async for chunk in chunks:
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise HTTPException(413, "单个附件不能超过 20 MiB")
                        if len(head) < 16:
                            head += chunk[:16 - len(head)]
                        digest.update(chunk)
                        file.write(chunk)
                    if not size:
                        raise HTTPException(400, "不能添加空文件")
                    file.flush()
                    os.fsync(file.fileno())
                    written = os.fstat(file.fileno())
                    with attachment_directory(workdir) as current:
                        opened, resolved = os.fstat(directory), os.fstat(current)
                        if (opened.st_dev, opened.st_ino) != (resolved.st_dev, resolved.st_ino):
                            raise HTTPException(409, "附件目录在上传期间被替换")
                    try:
                        os.link(temporary, key, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
                    except FileExistsError:
                        with open_file(directory, key) as existing:
                            if hashlib.file_digest(existing, "sha256").digest() != digest.digest():
                                raise HTTPException(409, "同一附件标识对应不同内容") from None
                    else:
                        published = os.stat(key, dir_fd=directory, follow_symlinks=False)
                        if (published.st_dev, published.st_ino) != (written.st_dev, written.st_ino):
                            os.unlink(key, dir_fd=directory)
                            raise HTTPException(409, "附件发布期间文件被替换")
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            raise
        raise HTTPException(409, "附件目录不可用") from exc
    return {"size": size, "sha256": digest.hexdigest(), "type": media_type(head), "path": f"attachments/{key}"}


class VerifyAttachments(BaseModel):
    agent_id: str
    session_id: str
    workspace_id: str
    keys: list[str] = Field(max_length=128)


def register_workspace_attachments(app, storage, bus):
    slots = asyncio.Semaphore(4)
    prefix = "/nexus/workspace-attachments"

    async def resolve(user_id, agent_id, session_id):
        session = await storage.get_session(user_id, agent_id, session_id)
        # AgentScope SQL 按用户和 Session ID 查找，不验证传入的 Agent ID。
        if not session or session.agent_id != agent_id:
            raise HTTPException(404, "会话不存在")
        return await app.state.workspace_manager.get_workspace(
            user_id, agent_id, session_id, session.config.workspace_id,
        )

    @app.put(f"{prefix}/groups/{{channel_id}}")
    async def group_workspace(
        channel_id: str = PathParam(pattern=GROUP_ID),
        user_id: str = Depends(get_current_user_id),
    ):
        agent_id = "nexus-group-resources"
        session_id = f"resources-{channel_id}"
        if not await storage.get_session(user_id, agent_id, session_id):
            await storage.upsert_session(
                user_id, agent_id, SessionConfig(workspace_id=session_id, name="群组附件"), session_id=session_id,
            )
        return {"agent_id": agent_id, "session_id": session_id}

    @app.put(f"{prefix}/{{key}}")
    async def upload(
        request: Request, agent_id: str, session_id: str,
        key: str = PathParam(pattern=ATTACHMENT_ID),
        user_id: str = Depends(get_current_user_id),
    ):
        if slots.locked():
            raise HTTPException(429, "附件上传繁忙")
        async with slots:
            workspace = await resolve(user_id, agent_id, session_id)
            return await save(workspace.workdir, key, request.stream())

    @app.get(f"{prefix}/{{key}}")
    async def download(
        agent_id: str, session_id: str,
        key: str = PathParam(pattern=ATTACHMENT_ID),
        user_id: str = Depends(get_current_user_id),
    ):
        workspace = await resolve(user_id, agent_id, session_id)
        try:
            file = open_attachment(workspace.workdir, key)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            raise HTTPException(410, "附件文件已失效") from exc

        def chunks():
            with file:
                yield from iter(lambda: file.read(CHUNK_BYTES), b"")

        return StreamingResponse(chunks(), media_type="application/octet-stream", headers={
            "Content-Length": str(os.fstat(file.fileno()).st_size),
        })

    @app.post(f"{prefix}/{{key}}/copy")
    async def copy(
        agent_id: str, session_id: str, target_agent_id: str, target_session_id: str,
        key: str = PathParam(pattern=ATTACHMENT_ID),
        user_id: str = Depends(get_current_user_id),
    ):
        if agent_id != "nexus-group-resources" or target_agent_id == agent_id:
            raise HTTPException(403, "附件复制仅支持群资源到员工会话")
        if slots.locked():
            raise HTTPException(429, "附件复制繁忙")
        async with slots, bus.acquire_lock(f"nexus:retire:{target_session_id}"):
            target = await resolve(user_id, target_agent_id, target_session_id)
            # 目标由 Nexus 校验群成员及讨论，固定文件标识使复制可重试。
            try:
                with open_attachment(target.workdir, key):
                    return {"copied": True}
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
            try:
                source = await resolve(user_id, agent_id, session_id)
                original = open_attachment(source.workdir, key)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                raise HTTPException(410, "群附件原件已失效") from exc
            with original as file:
                async def chunks():
                    while chunk := await asyncio.to_thread(file.read, CHUNK_BYTES):
                        yield chunk

                return await save(target.workdir, key, chunks())

    @app.post(f"{prefix}/verify")
    async def verify_files(
        body: VerifyAttachments,
        user_id: str = Depends(get_current_user_id),
    ):
        if any(not re.fullmatch(ATTACHMENT_ID, key) for key in body.keys):
            raise HTTPException(422, "附件标识无效")
        session = await storage.get_session(user_id, body.agent_id, body.session_id)
        if not session or session.agent_id != body.agent_id:
            raise HTTPException(404, "会话不存在")
        if session.config.workspace_id != body.workspace_id:
            raise HTTPException(409, "会话与工作区不匹配")
        workspace = await app.state.workspace_manager.get_workspace(
            user_id, session.agent_id, session.id, session.config.workspace_id,
        )
        missing = []
        for key in body.keys:
            try:
                with open_attachment(workspace.workdir, key):
                    pass
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                missing.append(key)
        return {"missing": missing}

    @app.delete(f"{prefix}/group-member", status_code=204)
    async def retire_group_member(
        agent_id: str, session_id: str, channel_id: str, employee_id: str,
        user_id: str = Depends(get_current_user_id),
    ):
        if not re.fullmatch(GROUP_ID, channel_id) or not re.fullmatch(r"[\w-]{1,64}", employee_id):
            raise HTTPException(422, "群成员标识无效")
        workspace_id = f"ws-{channel_id}-{employee_id}"
        async with bus.acquire_lock(f"nexus:retire:{session_id}"):
            session = await storage.get_session(user_id, agent_id, session_id)
            if not session:
                return
            if session.agent_id != agent_id or session.config.workspace_id != workspace_id:
                raise HTTPException(409, "附件副本会话不属于该群")
            sessions = app.state.session_service
            if not await sessions.cancel_session_run(session_id):
                raise HTTPException(409, "会话仍在运行，稍后重试回收")
            workspace = await app.state.workspace_manager.get_workspace(user_id, agent_id, session_id, workspace_id)
            try:
                with attachment_directory(workspace.workdir) as directory:
                    for key in os.listdir(directory):
                        if re.fullmatch(ATTACHMENT_ID, key):
                            os.unlink(key, dir_fd=directory)
            except FileNotFoundError:
                pass
            await sessions.delete_session(user_id, agent_id, session_id)

    @app.delete(f"{prefix}/{{key}}", status_code=204)
    async def delete(
        agent_id: str, session_id: str,
        key: str = PathParam(pattern=ATTACHMENT_ID),
        user_id: str = Depends(get_current_user_id),
    ):
        workspace = await resolve(user_id, agent_id, session_id)
        try:
            with attachment_directory(workspace.workdir) as directory:
                os.unlink(key, dir_fd=directory)
        except FileNotFoundError:
            pass
