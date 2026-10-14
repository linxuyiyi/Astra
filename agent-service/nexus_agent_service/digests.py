# -*- coding: utf-8 -*-
"""Nexus 消息与状态语义摘要计算（任务 3.2.3.1 观察基础）。

提供两个纯函数：
- ``message_digest(msg: Msg) -> str``: 消息语义内容摘要（SHA-256 规范 JSON）
- ``state_digest(state: AgentState) -> str``: 会话状态实际提交对象摘要（SHA-256 规范 JSON）

特性：
- 先核实属于已知公开 Block 类型，再用 model_dump(mode="json") 得到 JSON 安全投影；
- 按具体 Block 层级仅去掉原生构建时间字段（created_at / finished_at / Hint.finished_at）；
- 排除 ToolResultBlock.output 中原生临时生成的 TextBlock.id，但顶层 block ID 及 Hint 内部 block ID 完整保留；
- 保留 DataBlock source、ThinkingBlock extra 字段（如 signature / redacted_thinking_data）；
- 保留工具 input/output/state/metadata/suggested_rules，禁止递归删除任意 payload 内部同名 id/created_at；
- 排除 ChatService 在 observer 外填补的终态 finished_reason/error；
- 未知 block 类型明确拒绝（fail-closed）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from agentscope.message import (
    DataBlock,
    HintBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agentscope.state import AgentState

_KNOWN_BLOCK_TYPES = (
    TextBlock,
    ThinkingBlock,
    HintBlock,
    DataBlock,
    ToolCallBlock,
    ToolResultBlock,
)


def _project_block(block: Any) -> dict[str, Any]:
    """将公开 Block 投影为语义字典。

    先确认属于已知公开 block 类型，再通过 model_dump(mode='json') 获取安全投影；
    只在相应层级剔除原生构建时间戳，保留顶层及 Hint 内部 ID 与完整 extra/source。
    """
    if not isinstance(block, _KNOWN_BLOCK_TYPES):
        raise TypeError(f"未知或不受支持的 block 类型: {type(block).__name__}")

    dump: dict[str, Any] = block.model_dump(mode="json")
    dump.pop("created_at", None)
    dump.pop("finished_at", None)

    if isinstance(block, ToolResultBlock):
        output = dump.get("output")
        if isinstance(output, list):
            for item in output:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        # ToolResultBlock.output 中原生临时生成的 TextBlock.id 必须排除
                        item.pop("id", None)
                        item.pop("created_at", None)
                        item.pop("finished_at", None)
                    elif item.get("type") == "data":
                        item.pop("created_at", None)
                        item.pop("finished_at", None)
    elif isinstance(block, HintBlock):
        hint = dump.get("hint")
        if isinstance(hint, list):
            for item in hint:
                if isinstance(item, dict) and item.get("type") in ("text", "data"):
                    item.pop("created_at", None)
                    item.pop("finished_at", None)

    return dump


def message_digest(msg: Msg) -> str:
    """计算消息的语义内容摘要（SHA-256 十六进制）。

    保留消息 id/name/role、metadata、structured_output 以及有序 content。
    排除构建时间戳、finished_reason、error 以及 ToolResultBlock.output 内的临时 TextBlock.id。
    未知 block 类型直接抛出 TypeError（fail-closed）。
    """
    if not isinstance(msg, Msg):
        raise TypeError(f"预期 Msg 实例，收到: {type(msg).__name__}")

    content = getattr(msg, "content", []) or []
    if not isinstance(content, list):
        raise TypeError(f"Msg content 必须是 list，收到: {type(content).__name__}")

    projected_content = [_project_block(b) for b in content]

    dumped_fields = msg.model_dump(mode="json", include={"metadata", "structured_output"})

    projection: dict[str, Any] = {
        "id": msg.id,
        "name": msg.name,
        "role": msg.role,
        "metadata": dumped_fields.get("metadata") or {},
        "structured_output": dumped_fields.get("structured_output"),
        "content": projected_content,
    }

    serialized = json.dumps(
        projection,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def state_digest(state: AgentState) -> str:
    """计算 AgentState 实际提交对象的规范摘要（SHA-256 十六进制）。

    针对 state.model_dump(mode='json') 计算规范 JSON 哈希。
    同一个实际提交对象必须在入口处冻结计算。
    """
    if not isinstance(state, AgentState):
        raise TypeError(f"预期 AgentState 实例，收到: {type(state).__name__}")

    dumped = state.model_dump(mode="json")
    serialized = json.dumps(
        dumped,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
