"""Read a message's Nexus attachment only when the agent asks for it."""

import asyncio
import base64
import hashlib
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
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
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


def _is_image(name: str, media_type: str) -> bool:
    return media_type.startswith("image/") or PurePath(name).suffix.lower() in _IMAGE_SUFFIXES


def _encode_image_preview(raw: bytes) -> tuple[str | None, str | None]:
    try:
        with Image.open(BytesIO(raw)) as original:
            if original.width * original.height > 40_000_000:
                return None, "原图分辨率超过预览上限，请从对话附件查看原图。"
            original.thumbnail((1920, 1920))
            image = ImageOps.exif_transpose(original).convert("RGBA")
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return None, "无法生成图片预览，请从对话附件查看原图。"
    for edge in (1920, 1280, 960):
        background.thumbnail((edge, edge))
        out = BytesIO()
        background.save(out, format="JPEG", quality=80)
        if out.tell() <= _PREVIEW_BYTES:
            return base64.b64encode(out.getvalue()).decode("ascii"), None
    return None, "图片预览超过大小限制，请从对话附件查看原图。"


def _image_preview(raw: bytes, name: str) -> ToolChunk:
    encoded, error = _encode_image_preview(raw)
    if error or encoded is None:
        return _text(error or "无法生成图片预览，请从对话附件查看原图。")
    return ToolChunk(content=[DataBlock(
        name=name,
        source=Base64Source(media_type="image/jpeg", data=encoded),
    )])


def _text(text: str) -> ToolChunk:
    return ToolChunk(content=[TextBlock(text=text)])


def _render(raw: bytes, name: str, media_type: str, offset: int, page: int) -> ToolChunk:
    suffix = PurePath(name).suffix.lower()
    if _is_image(name, media_type):
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


def _extract_error_detail(response: httpx.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("detail"), str) and payload["detail"].strip():
        return payload["detail"].strip()
    return fallback


async def _read_image_attachment(
    nexus_api: str, agent_id: str, raw: bytes, name: str, question: str
) -> ToolChunk:
    encoded, error = await asyncio.to_thread(_encode_image_preview, raw)
    if error or encoded is None:
        return _text(error or "无法生成图片预览，请从对话附件查看原图。")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            capability_response = await client.get(
                f"{nexus_api}/api/v1/internal/agent-vision-capability",
                headers=headers(),
                params={"agent_id": agent_id},
            )
            capability_response.raise_for_status()
            capability = capability_response.json()
    except (httpx.HTTPError, ValueError):
        return _text("无法确认当前模型的图像识别能力，请稍后重试。")

    if bool(capability.get("supports_vision")):
        return ToolChunk(content=[DataBlock(
            name=name,
            source=Base64Source(media_type="image/jpeg", data=encoded),
        )])

    if not capability.get("vision_fallback_configured"):
        return _text(
            f"当前员工绑定的模型不支持原生图片识别，且平台尚未配置默认图像理解路由，暂时无法解析图片「{name}」。"
            "请联系管理员在 API 网关的「模型路由」页面配置默认图像理解路由。"
        )

    digest = hashlib.sha256(raw).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(65, connect=5)) as client:
            analyze_response = await client.post(
                f"{nexus_api}/api/v1/internal/vision/analyze",
                headers=headers(),
                json={
                    "image_base64": encoded,
                    "media_type": "image/jpeg",
                    "filename": name,
                    "question": question.strip(),
                    "sha256": digest,
                },
            )
    except httpx.TimeoutException:
        return _text(f"图片「{name}」自动视觉解析超时，请稍后重试。")
    except httpx.HTTPError:
        return _text(f"无法连接平台视觉解析服务，暂时无法解析图片「{name}」，请稍后重试。")

    if not analyze_response.is_success:
        detail = _extract_error_detail(analyze_response, f"HTTP {analyze_response.status_code}")
        return _text(f"图片「{name}」自动视觉解析失败：{detail}")

    try:
        result = analyze_response.json()
    except ValueError:
        return _text(f"图片「{name}」自动视觉解析返回格式异常。")
    text = str(result.get("text") or "").strip()
    model = str(result.get("model") or "").strip()
    if not text:
        return _text(f"图片「{name}」自动视觉解析未返回有效内容。")
    label = f"（由平台视觉模型 {model} 解析）" if model else ""
    return _text(f"图片附件 {name} 的视觉解析结果{label}：\n{text}")


async def read_attachment(
    storage,
    workspace_service,
    nexus_api,
    user_id,
    agent_id,
    session_id,
    message_id,
    attachment_id,
    offset,
    page,
    question: str = "",
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
    name, mime = item["name"], item["type"]

    if _is_image(name, mime):
        def load_image_bytes():
            with open_attachment(workspace.workdir, attachment_id) as file:
                return file.read()

        try:
            raw = await asyncio.to_thread(load_image_bytes)
        except HTTPException:
            return _text("无法读取此附件，文件已失效。")
        return await _read_image_attachment(nexus_api, agent_id, raw, name, question)

    def render():
        try:
            with open_attachment(workspace.workdir, attachment_id) as file:
                if mime.startswith("text/") or PurePath(name).suffix.lower() in _TEXT_SUFFIXES:
                    return _read_text(file, name, offset)
                return _render(file.read(), name, mime, offset, page)
        except HTTPException:
            return _text("无法读取此附件，文件已失效。")

    return await asyncio.to_thread(render)
