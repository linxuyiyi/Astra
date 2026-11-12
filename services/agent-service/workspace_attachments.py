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

from agentscope.app.deps import get_current_user_id, get_workspace_service
from agentscope.app.storage import SessionConfig
from fastapi import Depends, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

MAX_BYTES = 20 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
ATTACHMENT_ID = r"^att_[0-9a-f]{12,32}$"
GROUP_ID = r"^ch_[0-9a-f]{12,32}$"
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
WORKSPACE_SERVICE = Depends(get_workspace_service)


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


class LegacyMessage(BaseModel):
    agent_id: str
    session_id: str
    message_id: str
    aliases: dict[str, str]


def register_workspace_attachments(app, storage):
    slots = asyncio.Semaphore(4)
    prefix = "/nexus/workspace-attachments"

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

    @app.patch(f"{prefix}/legacy-message")
    async def rewrite_message(
        body: LegacyMessage,
        user_id: str = Depends(get_current_user_id),
        service=WORKSPACE_SERVICE,
    ):
        workspace = await service.resolve(user_id, body.agent_id, body.session_id)
        message = await storage.get_message(user_id, body.session_id, body.message_id)
        if not message or message.role != "user":
            raise HTTPException(404, "消息不存在")
        metadata = dict(message.metadata or {})
        attachments = metadata.get("attachments", [])
        for item in attachments:
            if item["id"] in body.aliases:
                key = body.aliases[item["id"]]
                if not re.fullmatch(ATTACHMENT_ID, key):
                    raise HTTPException(422, "附件标识无效")
                with open_attachment(workspace.workdir, key):
                    item["id"] = key
        metadata.pop("nexus_attachment_proofs", None)
        await storage.upsert_message(
            user_id, body.session_id, message.model_copy(update={"metadata": metadata}),
        )
        return {"updated": True}

    @app.put(f"{prefix}/{{key}}")
    async def upload(
        request: Request, agent_id: str, session_id: str,
        key: str = PathParam(pattern=ATTACHMENT_ID),
        user_id: str = Depends(get_current_user_id),
        service=WORKSPACE_SERVICE,
    ):
        if slots.locked():
            raise HTTPException(429, "附件上传繁忙")
        async with slots:
            workspace = await service.resolve(user_id, agent_id, session_id)
            return await save(workspace.workdir, key, request.stream())

    @app.get(f"{prefix}/{{key}}")
    async def download(
        agent_id: str, session_id: str,
        key: str = PathParam(pattern=ATTACHMENT_ID),
        user_id: str = Depends(get_current_user_id),
        service=WORKSPACE_SERVICE,
    ):
        workspace = await service.resolve(user_id, agent_id, session_id)
        file = open_attachment(workspace.workdir, key)

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
        service=WORKSPACE_SERVICE,
    ):
        if agent_id != "nexus-group-resources" or target_agent_id == agent_id:
            raise HTTPException(403, "附件复制仅支持群资源到员工会话")
        if slots.locked():
            raise HTTPException(429, "附件复制繁忙")
        async with slots:
            source = await service.resolve(user_id, agent_id, session_id)
            target = await service.resolve(user_id, target_agent_id, target_session_id)
            # 目标由 Nexus 校验群成员及讨论，固定文件标识使复制可重试。
            try:
                with open_attachment(target.workdir, key):
                    return {"copied": True}
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
            with open_attachment(source.workdir, key) as file:
                async def chunks():
                    while chunk := await asyncio.to_thread(file.read, CHUNK_BYTES):
                        yield chunk

                return await save(target.workdir, key, chunks())

    @app.delete(f"{prefix}/group-member", status_code=204)
    async def retire_group_member(
        agent_id: str, session_id: str, channel_id: str, employee_id: str,
        user_id: str = Depends(get_current_user_id),
    ):
        if not re.fullmatch(GROUP_ID, channel_id) or not re.fullmatch(r"[\w-]{1,64}", employee_id):
            raise HTTPException(422, "群成员标识无效")
        workspace_id = f"ws-{channel_id}-{employee_id}"
        session = await storage.get_session(user_id, agent_id, session_id)
        if session and session.config.workspace_id != workspace_id:
            raise HTTPException(409, "附件副本会话不属于该群")
        sessions = app.state.session_service
        if session and not await sessions.cancel_session_run(session_id):
            raise HTTPException(409, "会话仍在运行，稍后重试回收")
        workspace = await app.state.workspace_manager.get_workspace(user_id, agent_id, session_id, workspace_id)
        try:
            with attachment_directory(workspace.workdir) as directory:
                for key in os.listdir(directory):
                    if re.fullmatch(ATTACHMENT_ID, key):
                        os.unlink(key, dir_fd=directory)
        except FileNotFoundError:
            pass
        if session:
            await sessions.delete_session(user_id, agent_id, session_id)

    @app.delete(f"{prefix}/{{key}}", status_code=204)
    async def delete(
        agent_id: str, session_id: str,
        key: str = PathParam(pattern=ATTACHMENT_ID),
        user_id: str = Depends(get_current_user_id),
        service=WORKSPACE_SERVICE,
    ):
        workspace = await service.resolve(user_id, agent_id, session_id)
        try:
            with attachment_directory(workspace.workdir) as directory:
                os.unlink(key, dir_fd=directory)
        except FileNotFoundError:
            pass
