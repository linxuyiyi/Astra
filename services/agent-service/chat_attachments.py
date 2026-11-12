"""Read a message's Nexus attachment only when the agent asks for it."""

import asyncio
import base64
import json
import re
from io import BytesIO, TextIOWrapper
from pathlib import PurePath

import httpx
from agentscope.message import Base64Source, DataBlock, TextBlock
from agentscope.tool import ToolChunk
from fastapi import HTTPException
from group_security import execution_id, headers
from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from workspace_attachments import ATTACHMENT_ID, open_attachment

_TEXT_SUFFIXES = {
    ".txt", ".md", ".log", ".py", ".json", ".yaml", ".yml", ".js", ".ts", ".vue",
    ".css", ".html", ".xml", ".csv", ".sh", ".toml", ".ini", ".cfg", ".sql",
}
_TEXT_CHUNK = 8_000
_PREVIEW_BYTES = 512 * 1024


def with_attachment_hint(message):
    files = message.metadata.get("attachments")
    if not files:
        return message
    names = "\n".join(
        f"attachments/{item['id']} {json.dumps(item['name'], ensure_ascii=False)} ({item['type']})"
        for item in files
    )
    hint = TextBlock(
        id=f"nexus-attachments-{message.id}",
        text=f"消息 {message.id} 的会话附件（按需调用 read_chat_attachment）：\n{names}",
    )
    return message.model_copy(update={"content": [*message.content, hint]})


def _image_preview(raw: bytes, name: str) -> ToolChunk:
    try:
        with Image.open(BytesIO(raw)) as original:
            if original.width * original.height > 40_000_000:
                return _text("原图分辨率超过预览上限，请从对话附件查看原图。")
            original.thumbnail((1920, 1920))
            image = ImageOps.exif_transpose(original).convert("RGBA")
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return _text("无法生成图片预览，请从对话附件查看原图。")
    for edge in (1920, 1280, 960):
        background.thumbnail((edge, edge))
        out = BytesIO()
        background.save(out, format="JPEG", quality=80)
        if out.tell() <= _PREVIEW_BYTES:
            return ToolChunk(content=[DataBlock(
                name=name,
                source=Base64Source(media_type="image/jpeg", data=base64.b64encode(out.getvalue()).decode()),
            )])
    return _text("图片预览超过大小限制，请从对话附件查看原图。")


def _text(text: str) -> ToolChunk:
    return ToolChunk(content=[TextBlock(text=text)])


def _render(raw: bytes, name: str, media_type: str, offset: int, page: int) -> ToolChunk:
    suffix = PurePath(name).suffix.lower()
    if media_type.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return _image_preview(raw, name)
    if media_type == "application/pdf" or suffix == ".pdf":
        try:
            pdf = PdfReader(BytesIO(raw))
            if page > len(pdf.pages):
                return _text(f"PDF 没有第 {page} 页，共 {len(pdf.pages)} 页。")
            content = pdf.pages[page - 1].extract_text()
        except (PdfReadError, ValueError):
            return _text("无法读取此 PDF 的文本。")
        label = f"{name} 第 {page}/{len(pdf.pages)} 页"
    else:
        return _text("此附件格式不支持直接读取，用户仍可从对话中下载原件。")
    if offset >= len(content):
        return _text(f"{label}：此位置没有可读文本。")
    end = min(offset + _TEXT_CHUNK, len(content))
    next_offset = f"\n还有正文，下一次使用 offset={end}。" if end < len(content) else ""
    return _text(f"{label}\n{content[offset:end]}{next_offset}")


def _read_text(file, name, offset):
    try:
        with TextIOWrapper(file, encoding="utf-8", newline="") as text:
            remaining = offset
            while remaining:
                block = text.read(min(remaining, _TEXT_CHUNK))
                if not block:
                    break
                remaining -= len(block)
            content = text.read(_TEXT_CHUNK + 1)
    except UnicodeDecodeError:
        return _text("此附件不是 UTF-8 文本。")
    if not content:
        return _text(f"{name}：此位置没有可读文本。")
    next_offset = ""
    if len(content) > _TEXT_CHUNK:
        next_offset = f"\n还有正文，下一次使用 offset={offset + _TEXT_CHUNK}。"
    return _text(f"{name}\n{content[:_TEXT_CHUNK]}{next_offset}")


async def read_attachment(
    storage, workspace_service, nexus_api, user_id, agent_id, session_id, message_id, attachment_id, offset, page,
):
    if offset < 0 or page < 1:
        return _text("offset 必须大于等于 0，page 从 1 开始。")
    message = await storage.get_message(user_id, session_id, message_id)
    metadata = (message.metadata or {}) if message and message.role == "user" else {}
    item = next(
        (item for item in metadata.get("attachments", []) if isinstance(item, dict) and item.get("id") == attachment_id),
        {},
    )
    if not item or not re.fullmatch(ATTACHMENT_ID, attachment_id):
        return _text("当前会话的这条消息中没有此附件。")
    fence = execution_id.get()
    if fence:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{nexus_api}/api/v1/internal/chat-attachment-access",
                    headers=headers(), params={"session_id": session_id, "key": attachment_id, "execution_id": fence},
                )
                response.raise_for_status()
        except httpx.HTTPError:
            return _text("群附件不可访问。")
    workspace = await workspace_service.resolve(user_id, agent_id, session_id)

    def render():
        try:
            with open_attachment(workspace.workdir, attachment_id) as file:
                name, mime = item["name"], item["type"]
                if mime.startswith("text/") or PurePath(name).suffix.lower() in _TEXT_SUFFIXES:
                    return _read_text(file, name, offset)
                return _render(file.read(), name, mime, offset, page)
        except HTTPException:
            return _text("无法读取此附件，文件已失效。")

    return await asyncio.to_thread(render)
