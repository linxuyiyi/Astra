# -*- coding: utf-8 -*-
"""Nexus 消息与会话状态语义摘要单元测试（任务 3.2.3.1）。"""
from __future__ import annotations

import pytest
from agentscope.event import (
    ReplyFinishedReason,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import (
    AssistantMsg,
    Base64Source,
    DataBlock,
    HintBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
    URLSource,
)
from agentscope.permission import PermissionRule
from agentscope.state import AgentState

from nexus_agent_service.digests import message_digest, state_digest


def _build_streamed_reply(reply_id: str = "reply-test-1", result_text: str = "42") -> AssistantMsg:
    """使用真实公开 AssistantMsg 与 Msg.append_event 重建包含工具结果的回复。"""
    msg = AssistantMsg(id=reply_id, name="test_agent", content=[])
    msg.append_event(TextBlockStartEvent(session_id="s1", reply_id=reply_id, block_id="b-1", text=""))
    msg.append_event(TextBlockDeltaEvent(session_id="s1", reply_id=reply_id, block_id="b-1", delta="Calculating..."))
    msg.append_event(TextBlockEndEvent(session_id="s1", reply_id=reply_id, block_id="b-1"))

    msg.append_event(ToolCallStartEvent(session_id="s1", reply_id=reply_id, tool_call_id="call-1", tool_call_name="calculator"))
    msg.append_event(ToolCallDeltaEvent(session_id="s1", reply_id=reply_id, tool_call_id="call-1", delta='{"expr": "6*7"}'))
    msg.append_event(ToolCallEndEvent(session_id="s1", reply_id=reply_id, tool_call_id="call-1"))

    msg.append_event(ToolResultStartEvent(session_id="s1", reply_id=reply_id, tool_call_id="call-1", tool_call_name="calculator"))
    msg.append_event(ToolResultTextDeltaEvent(session_id="s1", reply_id=reply_id, tool_call_id="call-1", delta=result_text))
    msg.append_event(ToolResultEndEvent(session_id="s1", reply_id=reply_id, tool_call_id="call-1", state="success"))
    return msg


def test_event_rebuild_identical_digest_despite_random_nested_ids():
    """同组真实事件重建两次，原生 dump 因内部临时生成的 TextBlock.id 与时间戳而不同，但语义 digest 完全相同。"""
    m1 = _build_streamed_reply()
    m2 = _build_streamed_reply()

    # 原生 dump 不相同
    dump1 = m1.model_dump(mode="json")
    dump2 = m2.model_dump(mode="json")
    assert dump1 != dump2

    # 验证确实由 ToolResultBlock.output 内部临时 TextBlock.id 差异导致
    nested_t1 = m1.content[2].output[0]
    nested_t2 = m2.content[2].output[0]
    assert isinstance(nested_t1, TextBlock)
    assert isinstance(nested_t2, TextBlock)
    assert nested_t1.id != nested_t2.id

    # 规范化消息摘要必须严格相同
    d1 = message_digest(m1)
    d2 = message_digest(m2)
    assert d1 == d2
    assert isinstance(d1, str) and len(d1) == 64


def test_different_text_content_differs():
    """不同正文内容必须生成不同的 digest。"""
    m1 = AssistantMsg(id="r1", name="bot", content=[TextBlock(id="b1", text="Hello world")])
    m2 = AssistantMsg(id="r1", name="bot", content=[TextBlock(id="b1", text="Hello Nexus")])
    assert message_digest(m1) != message_digest(m2)


def test_different_tool_call_input_differs():
    """工具调用的输入不同，digest 必须不同。"""
    tc1 = ToolCallBlock(id="call-1", name="search", input='{"q": "weather"}')
    tc2 = ToolCallBlock(id="call-1", name="search", input='{"q": "finance"}')
    m1 = AssistantMsg(id="r1", name="bot", content=[tc1])
    m2 = AssistantMsg(id="r1", name="bot", content=[tc2])
    assert message_digest(m1) != message_digest(m2)


def test_tool_payload_embedded_id_and_created_at_preserved():
    """禁止递归删除工具 payload 内部的 id 或 created_at 字段。"""
    # 1. 验证 ToolResultBlock.metadata 内部保留 id 与 created_at
    res1 = ToolResultBlock(
        id="c1",
        name="api",
        metadata={"id": "doc-001", "created_at": "2026-10-15T00:00:00Z", "val": 10},
        output="ok",
        state="success",
    )
    res2 = ToolResultBlock(
        id="c1",
        name="api",
        metadata={"id": "doc-002", "created_at": "2026-10-15T00:00:00Z", "val": 10},
        output="ok",
        state="success",
    )
    res3 = ToolResultBlock(
        id="c1",
        name="api",
        metadata={"id": "doc-001", "created_at": "2026-10-16T00:00:00Z", "val": 10},
        output="ok",
        state="success",
    )
    m1 = AssistantMsg(id="r1", name="bot", content=[res1])
    m2 = AssistantMsg(id="r1", name="bot", content=[res2])
    m3 = AssistantMsg(id="r1", name="bot", content=[res3])

    assert message_digest(m1) != message_digest(m2)
    assert message_digest(m1) != message_digest(m3)

    # 2. 验证 ToolCallBlock.input 内部嵌入的 id 与 created_at 变更同样使 digest 改变
    tc1 = ToolCallBlock(id="c1", name="fetch", input='{"id": "rec-1", "created_at": "2026-01-01"}')
    tc2 = ToolCallBlock(id="c1", name="fetch", input='{"id": "rec-2", "created_at": "2026-01-01"}')
    assert message_digest(AssistantMsg(id="r1", name="bot", content=[tc1])) != message_digest(AssistantMsg(id="r1", name="bot", content=[tc2]))


def test_different_datablock_source_differs():
    """附件/数据块的实际 source（文本或二进制数据/URL）不同，digest 必须不同。"""
    d_b64_1 = DataBlock(
        id="d1",
        name="avatar.png",
        source=Base64Source(data="aGVsbG8=", media_type="image/png"),
    )
    d_b64_2 = DataBlock(
        id="d1",
        name="avatar.png",
        source=Base64Source(data="d29ybGQ=", media_type="image/png"),
    )
    d_url = DataBlock(
        id="d1",
        name="avatar.png",
        source=URLSource(url="https://example.com/img.png", media_type="image/png"),
    )

    m1 = AssistantMsg(id="r1", name="bot", content=[d_b64_1])
    m2 = AssistantMsg(id="r1", name="bot", content=[d_b64_2])
    m3 = AssistantMsg(id="r1", name="bot", content=[d_url])

    assert message_digest(m1) != message_digest(m2)
    assert message_digest(m1) != message_digest(m3)


def test_top_level_block_id_change_differs():
    """顶层 block ID 变化必须导致 digest 变化（与 ToolResult 内部临时 TextBlock.id 区分）。"""
    m1 = AssistantMsg(id="r1", name="bot", content=[TextBlock(id="blk-A", text="same content")])
    m2 = AssistantMsg(id="r1", name="bot", content=[TextBlock(id="blk-B", text="same content")])
    assert message_digest(m1) != message_digest(m2)


def test_chatservice_terminal_error_does_not_affect_message_digest():
    """ChatService 在 observer 外部补充的 ERROR 终态（finished_reason / error）不影响内容 digest。"""
    m_clean = _build_streamed_reply()
    d_clean = message_digest(m_clean)

    # 模拟 ChatService 在流中断后在 middleware 外部给 reply_msg 补充终态
    m_err = _build_streamed_reply()
    m_err.finished_reason = ReplyFinishedReason.ERROR
    m_err.error = {"message": "Stream disconnected", "type": "RuntimeError"}

    assert message_digest(m_err) == d_clean


def test_unknown_block_type_fails_closed():
    """遇到未知 block 类型必须抛出 TypeError（fail-closed），不能静默跳过漏掉内容。"""
    class CustomFakeBlock:
        def __init__(self):
            self.type = "custom"
            self.val = 123

    m = AssistantMsg(id="r1", name="bot", content=[])
    m.content.append(CustomFakeBlock())
    with pytest.raises(TypeError, match="未知或不受支持的 block 类型"):
        message_digest(m)


def test_state_digest_deterministic_and_sensitive_to_changes():
    """AgentState 规范 hash 对实际提交对象稳定，但对实际 state 内容变动敏感。"""
    s1 = AgentState(
        session_id="sess-1",
        summary="summary 1",
        context=[],
        reply_id="rep-1",
    )
    s2 = AgentState(
        session_id="sess-1",
        summary="summary 1",
        context=[],
        reply_id="rep-1",
    )
    s_modified = AgentState(
        session_id="sess-1",
        summary="summary 2",
        context=[],
        reply_id="rep-1",
    )

    d1 = state_digest(s1)
    d2 = state_digest(s2)
    d_mod = state_digest(s_modified)

    assert d1 == d2
    assert d1 != d_mod
    assert isinstance(d1, str) and len(d1) == 64


def test_hint_block_multimodal_nested_content_distinguishable():
    """HintBlock 多模态嵌套 TextBlock/DataBlock 正常序列化且内容/source 变化可区分。"""
    h1 = HintBlock(
        id="h-1",
        source="rag",
        hint=[
            TextBlock(id="t-1", text="Knowledge snippet A"),
            DataBlock(id="d-1", source=Base64Source(data="aW1nQQ==", media_type="image/png")),
        ],
    )
    h2 = HintBlock(
        id="h-1",
        source="rag",
        hint=[
            TextBlock(id="t-1", text="Knowledge snippet B"),
            DataBlock(id="d-1", source=Base64Source(data="aW1nQQ==", media_type="image/png")),
        ],
    )
    h3 = HintBlock(
        id="h-1",
        source="rag",
        hint=[
            TextBlock(id="t-1", text="Knowledge snippet A"),
            DataBlock(id="d-1", source=Base64Source(data="aW1nQkI=", media_type="image/png")),
        ],
    )

    m1 = AssistantMsg(id="r1", name="bot", content=[h1])
    m2 = AssistantMsg(id="r1", name="bot", content=[h2])
    m3 = AssistantMsg(id="r1", name="bot", content=[h3])

    d1 = message_digest(m1)
    d2 = message_digest(m2)
    d3 = message_digest(m3)

    assert d1 != d2
    assert d1 != d3
    assert d2 != d3


def test_tool_call_permission_rules_serializable_and_sensitive():
    """非空 PermissionRule 嵌套能够正常序列化，规则字段变化能产生不同摘要。"""
    rule_allow = PermissionRule(tool_name="calc", rule_content="*", behavior="allow", source="user")
    rule_deny = PermissionRule(tool_name="calc", rule_content="*", behavior="deny", source="user")
    rule_content = PermissionRule(tool_name="calc", rule_content="read", behavior="allow", source="user")

    tc1 = ToolCallBlock(id="c1", name="calc", input="{}", suggested_rules=[rule_allow])
    tc2 = ToolCallBlock(id="c1", name="calc", input="{}", suggested_rules=[rule_deny])
    tc3 = ToolCallBlock(id="c1", name="calc", input="{}", suggested_rules=[rule_content])

    m1 = AssistantMsg(id="r1", name="bot", content=[tc1])
    m2 = AssistantMsg(id="r1", name="bot", content=[tc2])
    m3 = AssistantMsg(id="r1", name="bot", content=[tc3])

    d1 = message_digest(m1)
    d2 = message_digest(m2)
    d3 = message_digest(m3)

    assert d1 != d2
    assert d1 != d3
    assert d2 != d3


def test_thinking_block_extra_fields_preserved_and_sensitive():
    """ThinkingBlock 的 extra 字段（如 signature 与 redacted_thinking_data）完整保留且变动敏感。"""
    th_base = ThinkingBlock(id="th-1", thinking="deep thought")
    th_sig1 = ThinkingBlock(id="th-1", thinking="deep thought", signature="sig-alpha")
    th_sig2 = ThinkingBlock(id="th-1", thinking="deep thought", signature="sig-beta")
    th_red1 = ThinkingBlock(id="th-1", thinking="deep thought", redacted_thinking_data="redacted-A")
    th_red2 = ThinkingBlock(id="th-1", thinking="deep thought", redacted_thinking_data="redacted-B")

    m_base = AssistantMsg(id="r1", name="bot", content=[th_base])
    m_sig1 = AssistantMsg(id="r1", name="bot", content=[th_sig1])
    m_sig2 = AssistantMsg(id="r1", name="bot", content=[th_sig2])
    m_red1 = AssistantMsg(id="r1", name="bot", content=[th_red1])
    m_red2 = AssistantMsg(id="r1", name="bot", content=[th_red2])

    d_base = message_digest(m_base)
    d_sig1 = message_digest(m_sig1)
    d_sig2 = message_digest(m_sig2)
    d_red1 = message_digest(m_red1)
    d_red2 = message_digest(m_red2)

    assert d_base != d_sig1
    assert d_sig1 != d_sig2
    assert d_base != d_red1
    assert d_red1 != d_red2


def test_metadata_pydantic_values_serializable_and_equivalent_to_dict():
    """metadata 合法包含 Pydantic 对象可摘要，与原生 JSON 等价 dict 相同，规则改变摘要不同。"""
    rule = PermissionRule(tool_name="calc", rule_content="*", behavior="allow", source="user")
    rule_dict = {"tool_name": "calc", "rule_content": "*", "behavior": "allow", "source": "user"}

    m_pydantic = AssistantMsg(
        id="r1",
        name="bot",
        content=[TextBlock(id="b1", text="hi")],
        metadata={"perm": rule},
    )
    m_dict = AssistantMsg(
        id="r1",
        name="bot",
        content=[TextBlock(id="b1", text="hi")],
        metadata={"perm": rule_dict},
    )

    # 与原生 JSON 安全等价 dict 摘要相同
    assert message_digest(m_pydantic) == message_digest(m_dict)

    # 内部规则改变摘要不同
    rule_modified = PermissionRule(tool_name="calc", rule_content="read", behavior="allow", source="user")
    m_modified = AssistantMsg(
        id="r1",
        name="bot",
        content=[TextBlock(id="b1", text="hi")],
        metadata={"perm": rule_modified},
    )
    assert message_digest(m_pydantic) != message_digest(m_modified)


def test_structured_output_pydantic_values_serializable_and_equivalent_to_dict():
    """structured_output 合法包含 Pydantic 对象可摘要，与原生 JSON 等价 dict 相同，规则改变摘要不同。"""
    rule = PermissionRule(tool_name="calc", rule_content="*", behavior="allow", source="user")
    rule_dict = {"tool_name": "calc", "rule_content": "*", "behavior": "allow", "source": "user"}

    m_pydantic = AssistantMsg(
        id="r1",
        name="bot",
        content=[TextBlock(id="b1", text="hi")],
        structured_output={"decision": rule},
    )
    m_dict = AssistantMsg(
        id="r1",
        name="bot",
        content=[TextBlock(id="b1", text="hi")],
        structured_output={"decision": rule_dict},
    )

    # 与原生 JSON 安全等价 dict 摘要相同
    assert message_digest(m_pydantic) == message_digest(m_dict)

    # 内部规则改变摘要不同
    rule_modified = PermissionRule(tool_name="calc", rule_content="read", behavior="allow", source="user")
    m_modified = AssistantMsg(
        id="r1",
        name="bot",
        content=[TextBlock(id="b1", text="hi")],
        structured_output={"decision": rule_modified},
    )
    assert message_digest(m_pydantic) != message_digest(m_modified)
