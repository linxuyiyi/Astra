# -*- coding: utf-8 -*-
"""知识库真实检索观察与运行 guard 生命周期测试（OpenSpec 任务 3.2.2）。

失败/边界测试**不依赖字符串假事件**：使用真实公开 ``Agent`` + 原生
``RAGMiddleware`` + 真实 ``ModelCallStartEvent`` 类型，并以 recording model 计数
证明“审计不满足时模型 0 调用”，同时核对原生 Hint 清理与 ContextVar 结束清理。
真实 AI 联调不在此处，见 ``scripts/smoke_knowledge.py``。
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from agentscope.agent import Agent
from agentscope.event import HintBlockEvent, ModelCallStartEvent
from agentscope.message import Msg, TextBlock
from agentscope.middleware import RAGMiddleware
from agentscope.model import ChatModelBase
from agentscope.rag import (
    Chunk,
    KnowledgeBase,
    VectorSearchResult,
)
from agentscope.tool import Toolkit

from nexus_agent_service import knowledge
from nexus_agent_service.guard import (
    RunAuthorization,
    RunGuardMiddleware,
    RunNotAuthorizedError,
)
from nexus_agent_service.knowledge import (
    KnowledgeSearchObservation,
    ObservedKnowledgeBase,
    UnexpectedKnowledgeSearchError,
)

STATIC_PARAMS = {"mode": "static", "top_k": 3, "emit_hint_event": True}


# ----------------------------------------------------------------------
# 观察记录与包装
# ----------------------------------------------------------------------
def _inner_kb():
    return KnowledgeBase(
        name="kb",
        description="d",
        embedding_model=object(),
        vector_store=object(),
        collection="c",
    )


def test_observation_reports_missing_and_failed():
    obs = KnowledgeSearchObservation(expected=frozenset({"a", "b", "c"}))
    obs.record_success("a")
    obs.record_failure("b", "ConnectError")
    assert obs.missing() == ["c"]
    assert obs.failed() == {"b": "ConnectError"}
    assert obs.unexpected() == []
    reason = obs.unsatisfied_reason()
    assert reason is not None and "b" in reason and "c" in reason


def test_observation_flags_unselected_kb_drift():
    """原生配置漂移：出现未选 KB 参与检索必须判定为不满足。"""
    obs = KnowledgeSearchObservation(expected=frozenset({"a"}))
    obs.record_success("a")
    obs.record_success("extra")
    assert obs.unexpected() == ["extra"]
    assert obs.missing() == []
    assert obs.unsatisfied_reason() is not None


@pytest.mark.asyncio
async def test_observed_kb_delegates_and_records_success():
    inner = _inner_kb()

    async def fake_search(queries, top_k=5, score_threshold=None):
        return ["hit"]

    inner.search = fake_search
    observed = ObservedKnowledgeBase(inner, "kb-1")
    token = knowledge.set_observation(
        KnowledgeSearchObservation(expected=frozenset({"kb-1"})),
    )
    try:
        result = await observed.search(["q"])
        obs = knowledge.current_observation()
        assert result == ["hit"]
        assert obs.succeeded == {"kb-1"}
        assert obs.unsatisfied_reason() is None
    finally:
        knowledge.reset_observation(token)


@pytest.mark.asyncio
async def test_observed_kb_records_failure_and_reraises():
    inner = _inner_kb()

    async def boom(queries, top_k=5, score_threshold=None):
        raise ConnectionError("no embedding")

    inner.search = boom
    observed = ObservedKnowledgeBase(inner, "kb-1")
    token = knowledge.set_observation(
        KnowledgeSearchObservation(expected=frozenset({"kb-1"})),
    )
    try:
        with pytest.raises(ConnectionError):
            await observed.search(["q"])
        obs = knowledge.current_observation()
        assert obs.failed() == {"kb-1": "ConnectionError"}
        assert obs.missing() == []
        assert obs.unsatisfied_reason() is not None
    finally:
        knowledge.reset_observation(token)


@pytest.mark.asyncio
async def test_observed_kb_rejects_unexpected_before_inner():
    """受控运行中未选 KB 必须在调用原生 search 之前拒绝，绝不真正检索。"""
    inner = _inner_kb()
    called = {"n": 0}

    async def fake_search(queries, top_k=5, score_threshold=None):
        called["n"] += 1
        return ["leak"]

    inner.search = fake_search
    observed = ObservedKnowledgeBase(inner, "kb-b")
    token = knowledge.set_observation(
        KnowledgeSearchObservation(expected=frozenset({"kb-a"})),
    )
    try:
        with pytest.raises(UnexpectedKnowledgeSearchError):
            await observed.search(["q"])
        assert called["n"] == 0
        obs = knowledge.current_observation()
        assert obs.unexpected() == ["kb-b"]
        assert obs.unsatisfied_reason() is not None
    finally:
        knowledge.reset_observation(token)


@pytest.mark.asyncio
async def test_standalone_search_without_run_observation_still_searches():
    """无运行观察（独立 REST/管理检索）仍走原生真正检索，不被封死。"""
    inner = _inner_kb()

    async def fake_search(queries, top_k=5, score_threshold=None):
        return ["native"]

    inner.search = fake_search
    observed = ObservedKnowledgeBase(inner, "kb-1")
    assert knowledge.current_observation() is None
    assert await observed.search(["q"]) == ["native"]


@pytest.mark.asyncio
async def test_observation_shared_across_concurrent_searches():
    """原生 static 检索经 gather 并发；子任务共享同一可变记录对象。"""
    obs = KnowledgeSearchObservation(expected=frozenset({"a", "b"}))
    token = knowledge.set_observation(obs)

    async def one(kb_id):
        awaited = ObservedKnowledgeBase(_inner_kb(), kb_id)

        async def fake_search(queries, top_k=5, score_threshold=None):
            await asyncio.sleep(0)
            return []

        awaited._inner.search = fake_search
        return await awaited.search(["q"])

    try:
        await asyncio.gather(one("a"), one("b"))
        assert obs.succeeded == {"a", "b"}
        assert obs.unsatisfied_reason() is None
    finally:
        knowledge.reset_observation(token)


# ----------------------------------------------------------------------
# 真实 Agent + 原生 RAGMiddleware + recording model
# ----------------------------------------------------------------------
def _hit(text="doc-value"):
    return VectorSearchResult(
        score=0.9,
        document_id="doc-1",
        chunk=Chunk(
            content=TextBlock(text=text),
            source="f.txt",
            chunk_index=0,
            total_chunks=1,
        ),
    )


def _observed_kb(kb_id, *, results=None, error=None, calls=None):
    inner = KnowledgeBase(
        name=kb_id,
        description="d",
        embedding_model=object(),
        vector_store=object(),
        collection=f"c-{kb_id}",
    )

    async def fake_search(queries, top_k=5, score_threshold=None):
        if calls is not None:
            calls.append(kb_id)
        if error is not None:
            raise error
        return list(results or [])

    inner.search = fake_search
    return ObservedKnowledgeBase(inner, kb_id)


class RecordingModel(ChatModelBase):
    class Parameters(BaseModel):
        pass

    def __init__(self):
        from agentscope.formatter import DeepSeekChatFormatter

        self.credential = None
        self.model = "recording"
        self.parameters = RecordingModel.Parameters()
        self.stream = False
        self.max_retries = 0
        self.retry_delay = 0
        self.context_size = 32768
        self.formatter = DeepSeekChatFormatter()
        self.calls = 0

    async def __call__(self, messages=None, tools=None, tool_choice=None):
        self.calls += 1
        from agentscope.model import ChatResponse

        return ChatResponse(
            content=[TextBlock(text="ANSWER")],
            is_last=True,
        )


class _Authorizer:
    def __init__(self, authorization):
        self._authorization = authorization

    async def authorize(self, user_id, agent_id, session_id):
        return self._authorization


class _MutableAuthorizer:
    def __init__(self, authorization):
        self._authorization = authorization

    async def authorize(self, user_id, agent_id, session_id):
        return self._authorization


class _Workspace:
    async def list_skills(self, agent_id=None):
        return []

    async def list_mcps(self, agent_id=None, session_id=None):
        return []


def _build_agent(model, authorizer, rag_kbs=None):
    middlewares = [
        RunGuardMiddleware(
            authorizer,
            "nexus-service",
            "agent-1",
            "sess-1",
            _Workspace(),
        ),
    ]
    if rag_kbs is not None:
        middlewares.append(
            RAGMiddleware(
                knowledge_bases=rag_kbs,
                parameters=RAGMiddleware.Parameters(**STATIC_PARAMS),
            ),
        )
    return Agent(
        name="recording",
        system_prompt="p",
        model=model,
        toolkit=Toolkit(tools=[]),
        middlewares=middlewares,
    )


async def _drive(agent):
    msg = Msg(name="user", role="user", content=[TextBlock(text="hi")])
    events: list = []
    error: BaseException | None = None
    try:
        async for event in agent.reply_stream(msg):
            events.append(event)
    except BaseException as exc:  # noqa: BLE001 - 记录后断言
        error = exc
    return events, error


def _context_block_ids(agent) -> set[str]:
    return {
        block.id
        for message in agent.state.context
        for block in message.content
    }


def _rag_hint_ids(events) -> set[str]:
    # 只认原生 RAG 的 Hint（source 标注 KnowledgeBase）；运行态注入的 Hint
    # 属于 Agent 机制、按设计保留，不能与 RAG 清理混为一谈。
    return {
        event.block_id
        for event in events
        if isinstance(event, HintBlockEvent)
        and isinstance(event.source, str)
        and "KnowledgeBase" in event.source
    }


def _rag_hint_remaining(agent, events) -> bool:
    """原生 RAG 注入的 Hint（按事件 block_id 精确识别）是否仍在上下文中。"""
    return bool(_rag_hint_ids(events) & _context_block_ids(agent))


@pytest.mark.asyncio
async def test_partial_missing_kb_suppresses_start_and_cleans_hint():
    """部分 KB 漏装且 Hint 已生成：模型 0 调用、START 被抑制、Hint 清理。"""
    model = RecordingModel()
    kb_a = _observed_kb("kb-a", results=[_hit("secret-doc-value")])
    # RAG 只装配 kb-a（kb-b 漏装），但可信授权要求 kb-a+kb-b。
    agent = _build_agent(
        model,
        _Authorizer(
            RunAuthorization(
                allowed=True,
                reason="ok",
                knowledge=("kb-a", "kb-b"),
            ),
        ),
        rag_kbs=[kb_a],
    )
    events, error = await _drive(agent)
    assert isinstance(error, RunNotAuthorizedError)
    assert model.calls == 0
    assert not any(isinstance(e, ModelCallStartEvent) for e in events)
    # Hint 确实生成过（证明是“已注入后仍中止”的清理边界）。
    assert _rag_hint_ids(events)
    assert not _rag_hint_remaining(agent, events)
    assert knowledge.current_observation() is None


@pytest.mark.asyncio
async def test_partial_failed_kb_suppresses_start_and_model_zero():
    """部分 KB 检索失败：模型 0 调用、START 被抑制、ContextVar 清理。"""
    model = RecordingModel()
    kb_a = _observed_kb("kb-a", results=[_hit()])
    kb_b = _observed_kb("kb-b", error=ConnectionError("down"))
    agent = _build_agent(
        model,
        _Authorizer(
            RunAuthorization(
                allowed=True,
                reason="ok",
                knowledge=("kb-a", "kb-b"),
            ),
        ),
        rag_kbs=[kb_a, kb_b],
    )
    events, error = await _drive(agent)
    assert isinstance(error, RunNotAuthorizedError)
    assert model.calls == 0
    assert not any(isinstance(e, ModelCallStartEvent) for e in events)
    assert not _rag_hint_remaining(agent, events)
    assert knowledge.current_observation() is None


@pytest.mark.asyncio
async def test_unexpected_native_kb_never_searched_and_model_zero():
    """snapshot=[] 但原生装配了 B：B 不真正检索、模型 0 调用。"""
    model = RecordingModel()
    calls: list[str] = []
    kb_b = _observed_kb("kb-b", results=[_hit("leak")], calls=calls)
    agent = _build_agent(
        model,
        _Authorizer(RunAuthorization(allowed=True, reason="ok")),
        rag_kbs=[kb_b],
    )
    events, error = await _drive(agent)
    assert isinstance(error, RunNotAuthorizedError)
    assert calls == []  # B.inner.search 从未被调用
    assert model.calls == 0
    assert not any(isinstance(e, ModelCallStartEvent) for e in events)
    assert knowledge.current_observation() is None


@pytest.mark.asyncio
async def test_satisfied_search_reaches_model_and_keeps_no_leak():
    model = RecordingModel()
    kb_a = _observed_kb("kb-a", results=[_hit("v")])
    agent = _build_agent(
        model,
        _Authorizer(
            RunAuthorization(
                allowed=True,
                reason="ok",
                knowledge=("kb-a",),
            ),
        ),
        rag_kbs=[kb_a],
    )
    events, error = await _drive(agent)
    assert error is None
    assert model.calls == 1
    assert any(isinstance(e, ModelCallStartEvent) for e in events)
    assert _rag_hint_ids(events)
    assert not _rag_hint_remaining(agent, events)  # static 默认 persist_hint=False，回合结束清理
    assert knowledge.current_observation() is None


@pytest.mark.asyncio
async def test_no_knowledge_run_continues_to_model():
    model = RecordingModel()
    agent = _build_agent(
        model,
        _Authorizer(RunAuthorization(allowed=True, reason="ok")),
        rag_kbs=None,
    )
    events, error = await _drive(agent)
    assert error is None
    assert model.calls == 1
    assert any(isinstance(e, ModelCallStartEvent) for e in events)
    assert knowledge.current_observation() is None


@pytest.mark.asyncio
async def test_failure_then_next_round_independent():
    """失败轮后下一轮独立：ContextVar 已清理，下一轮正常进入模型。"""
    model = RecordingModel()
    kb_a = _observed_kb("kb-a", results=[_hit("v")])
    authorizer = _MutableAuthorizer(
        RunAuthorization(
            allowed=True,
            reason="ok",
            knowledge=("kb-a", "kb-b"),
        ),
    )
    agent = _build_agent(model, authorizer, rag_kbs=[kb_a])

    _, error = await _drive(agent)
    assert isinstance(error, RunNotAuthorizedError)
    assert model.calls == 0
    assert knowledge.current_observation() is None

    # 修正为与原生装配一致的授权后，下一轮必须正常。
    authorizer._authorization = RunAuthorization(
        allowed=True,
        reason="ok",
        knowledge=("kb-a",),
    )
    _, error2 = await _drive(agent)
    assert error2 is None
    assert model.calls == 1


@pytest.mark.asyncio
async def test_concurrent_sessions_observations_independent():
    """并发两个 Session：各自观察独立，一失败一成功互不影响。"""
    fail_model = RecordingModel()
    ok_model = RecordingModel()
    fail_agent = _build_agent(
        fail_model,
        _Authorizer(
            RunAuthorization(
                allowed=True,
                reason="ok",
                knowledge=("kb-a", "kb-b"),
            ),
        ),
        rag_kbs=[_observed_kb("kb-a", results=[_hit()])],
    )
    ok_agent = _build_agent(
        ok_model,
        _Authorizer(
            RunAuthorization(
                allowed=True,
                reason="ok",
                knowledge=("kb-a",),
            ),
        ),
        rag_kbs=[_observed_kb("kb-a", results=[_hit()])],
    )

    (fail_events, fail_error), (ok_events, ok_error) = await asyncio.gather(
        _drive(fail_agent),
        _drive(ok_agent),
    )
    assert isinstance(fail_error, RunNotAuthorizedError)
    assert fail_model.calls == 0
    assert not any(
        isinstance(e, ModelCallStartEvent) for e in fail_events
    )
    assert ok_error is None
    assert ok_model.calls == 1
    assert any(isinstance(e, ModelCallStartEvent) for e in ok_events)
    assert knowledge.current_observation() is None


@pytest.mark.asyncio
async def test_guard_on_model_call_requires_observation_fail_closed():
    """缺失本轮观察（非受控路径）时 on_model_call 必须 fail-closed。"""
    mw = RunGuardMiddleware(
        _Authorizer(RunAuthorization(allowed=True, reason="ok")),
        "nexus-service",
        "agent-1",
        "sess-1",
        _Workspace(),
    )
    mw._authorization = RunAuthorization(allowed=True, reason="ok")

    async def model_next(**kwargs):
        raise AssertionError("缺失观察时不得调用模型")

    with pytest.raises(RunNotAuthorizedError):
        await mw.on_model_call(object(), {}, model_next)


@pytest.mark.asyncio
async def test_snapshot_a_native_ab_never_searches_b_and_model_zero():
    """snapshot=[A] 但原生装配 [A,B]：未选 B 不真正检索、模型 0 调用。"""
    model = RecordingModel()
    calls: list[str] = []
    kb_a = _observed_kb("kb-a", results=[_hit("v")])
    kb_b = _observed_kb("kb-b", results=[_hit("leak")], calls=calls)
    agent = _build_agent(
        model,
        _Authorizer(
            RunAuthorization(
                allowed=True,
                reason="ok",
                knowledge=("kb-a",),
            ),
        ),
        rag_kbs=[kb_a, kb_b],
    )
    events, error = await _drive(agent)
    assert isinstance(error, RunNotAuthorizedError)
    assert calls == []  # B.inner.search 从未被调用
    assert model.calls == 0
    assert not any(isinstance(e, ModelCallStartEvent) for e in events)
    assert knowledge.current_observation() is None


@pytest.mark.asyncio
async def test_zero_result_search_continues_to_model():
    """零结果的成功检索允许继续进入模型。"""
    model = RecordingModel()
    kb_a = _observed_kb("kb-a", results=[])
    agent = _build_agent(
        model,
        _Authorizer(
            RunAuthorization(
                allowed=True,
                reason="ok",
                knowledge=("kb-a",),
            ),
        ),
        rag_kbs=[kb_a],
    )
    events, error = await _drive(agent)
    assert error is None
    assert model.calls == 1
    assert any(isinstance(e, ModelCallStartEvent) for e in events)
    assert _rag_hint_ids(events) == set()  # 零结果不注入 RAG Hint
    assert knowledge.current_observation() is None


@pytest.mark.asyncio
async def test_missing_rag_middleware_suppresses_start():
    """所选 KB 但整套 RAG 漏装：模型 0 调用、START 被抑制。"""
    model = RecordingModel()
    agent = _build_agent(
        model,
        _Authorizer(
            RunAuthorization(
                allowed=True,
                reason="ok",
                knowledge=("kb-a",),
            ),
        ),
        rag_kbs=None,
    )
    events, error = await _drive(agent)
    assert isinstance(error, RunNotAuthorizedError)
    assert model.calls == 0
    assert not any(isinstance(e, ModelCallStartEvent) for e in events)
    assert knowledge.current_observation() is None
