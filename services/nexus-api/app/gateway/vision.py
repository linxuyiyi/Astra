"""图像理解公共协议：OpenAI 消息图片检测、结构化识图提示词与文本回写。

网关数据面视觉桥接与服务端附件识图共用此模块，确保识图提示词、缓存键和
图片回写格式只有一个事实源。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

VISION_CACHE_TTL_SECONDS = 24 * 3600
MAX_VISION_IMAGES_PER_REQUEST = 8

_VISION_SYSTEM_PROMPT = (
    "你是一个高精度图像理解助手，正在为下游纯文本大模型解析用户发送的图片。"
    "请严格依据图片真实可见内容作答，不得编造看不清或不存在的文字与数据；"
    "若部分细节模糊，请明确说明看不清。"
)

_DEFAULT_VISION_PROMPT = (
    "请全面且客观地解析这张图片，按以下结构输出简体中文内容：\n"
    "1. 画面核心摘要：一句话说明图片类型（如报错截图、界面截图、表格、架构图、单据、照片等）与主题。\n"
    "2. 可见文字与代码/报错原文：逐字准确提取图片中的关键文字、代码、终端日志、错误信息或数值，"
    "保持原有层级与标点。\n"
    "3. 关键界面与结构要素：描述重要图表趋势、表格行列关系、按钮/高亮状态或布局问题（如适用）。"
)


def image_part_url(part: Any) -> str | None:
    """提取 OpenAI Chat Completions 消息块中的图片 URL / data URI。"""

    if not isinstance(part, Mapping):
        return None
    kind = part.get("type")
    if kind == "image_url":
        image_obj = part.get("image_url")
        if isinstance(image_obj, Mapping):
            url = image_obj.get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()
        elif isinstance(image_obj, str) and image_obj.strip():
            return image_obj.strip()
    elif kind == "input_image":
        url = part.get("image_url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def _message_has_image(message: Any) -> bool:
    if not isinstance(message, Mapping):
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(image_part_url(part) is not None for part in content)


def payload_has_images(payload: Mapping[str, Any]) -> bool:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    return any(_message_has_image(message) for message in messages)


def current_turn_has_image(payload: Mapping[str, Any]) -> bool:
    """判断最后一轮（最近一次 assistant 回复之后）是否包含图片。"""

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False
    for message in reversed(messages):
        if not isinstance(message, Mapping):
            continue
        if message.get("role") == "assistant":
            break
        if _message_has_image(message):
            return True
    return False


def extract_image_urls(payload: Mapping[str, Any]) -> list[str]:
    """按出现顺序提取去重后的图片 URL / data URI。"""

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            url = image_part_url(part)
            if url is not None and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def vision_cache_key(
    route_model: str,
    target_model: str | None,
    image_ref: str,
    question: str = "",
) -> str:
    """按视觉路由模型、图片内容/摘要与问题生成稳定缓存键。"""

    hasher = hashlib.sha256()
    for segment in (route_model.strip(), (target_model or "").strip(), question.strip(), image_ref):
        hasher.update(segment.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


def build_vision_messages(
    image_url: str,
    *,
    filename: str = "",
    question: str = "",
) -> list[dict[str, Any]]:
    clean_name = filename.strip()
    clean_question = question.strip()
    header = f"图片文件名：{clean_name}\n" if clean_name else ""
    if clean_question:
        prompt = (
            f"{header}请重点围绕以下问题解析这张图片，并逐字引用图中相关文字、报错或数据作为依据：\n"
            f"{clean_question}\n\n"
            "如果图中还有与该问题直接相关的重要上下文，也请简要补充。"
        )
    else:
        prompt = f"{header}{_DEFAULT_VISION_PROMPT}"
    return [
        {"role": "system", "content": _VISION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]


def extract_vision_text(payload: Any) -> str | None:
    """从 OpenAI Chat Completions 响应体提取纯文本结果。"""

    if not isinstance(payload, Mapping):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    message = first.get("message")
    if not isinstance(message, Mapping):
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping) and item.get("type") == "text":
                piece = item.get("text")
                if isinstance(piece, str) and piece.strip():
                    parts.append(piece.strip())
        merged = "\n".join(parts).strip()
        return merged or None
    return None


def rewrite_payload_images(
    payload: Mapping[str, Any],
    descriptions: Mapping[str, str],
    *,
    vision_model: str | None = None,
    omitted_placeholder: str | None = None,
) -> dict[str, Any]:
    """将消息中的图片块替换为纯文本解析结果，并对纯文本消息折叠为字符串。"""

    updated = dict(payload)
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return updated

    header = (
        f"[图片内容解析（由平台视觉模型 {vision_model} 自动识别）]"
        if vision_model
        else "[图片内容解析]"
    )
    rewritten_messages: list[Any] = []
    for message in messages:
        if not isinstance(message, Mapping) or not isinstance(message.get("content"), list):
            rewritten_messages.append(message)
            continue
        new_parts: list[Any] = []
        replaced_any = False
        for part in message["content"]:
            url = image_part_url(part)
            if url is None:
                new_parts.append(part)
                continue
            replaced_any = True
            if url in descriptions:
                text = f"{header}\n{descriptions[url]}"
            elif omitted_placeholder:
                text = omitted_placeholder
            else:
                text = "[图片已省略]"
            new_parts.append({"type": "text", "text": text})
        if not replaced_any:
            rewritten_messages.append(message)
            continue
        new_message = dict(message)
        if all(isinstance(item, Mapping) and item.get("type") == "text" for item in new_parts):
            new_message["content"] = "\n\n".join(
                str(item.get("text") or "") for item in new_parts if str(item.get("text") or "")
            )
        else:
            new_message["content"] = new_parts
        rewritten_messages.append(new_message)

    updated["messages"] = rewritten_messages
    return updated
