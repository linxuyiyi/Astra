# -*- coding: utf-8 -*-
"""Knowledge 真实检索的最小公开观察扩展（OpenSpec 任务 3.2.2）。

官方装配（``agentscope.app._service``）会**跳过**无法解析的 KB，并在 static
检索异常时 log-and-skip 继续裸模型调用；这会让“所选知识库失效”与“成功回答”
无法区分。本模块只通过公开扩展点补上**观察**，不重做 RAG、不另存检索副本、
不引入第二套状态机：

* :class:`ObservedCollectionPerKbManager` 继承公开
  ``CollectionPerKbManager``，仅覆写 ``get_knowledge``，把原生返回的公开
  :class:`~agentscope.rag.KnowledgeBase` 包在 :class:`ObservedKnowledgeBase`
  里；``search`` **仍委托原生实现**，只在成功/失败时记录本轮观察。
* 观察记录存放在模块级 :data:`ContextVar`，由运行 guard 的 ``on_reply`` 设置
  为本轮临时对象、``finally`` 清理；原生 static 检索会经 ``asyncio.gather``
  并发调用各 KB 的 ``search``，子任务复制上下文时**共享同一个可变记录对象**，
  因此并发结果都能回写，且不会跨轮/session 串味。
* 是否“全部成功”由 guard 在模型调用前用**可信授权快照**中的 KB id 判定；
  本模块只如实记录，不自行放行。
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field

from agentscope.app.rag.knowledge_base_manager import CollectionPerKbManager
from agentscope.rag import KnowledgeBase


class UnexpectedKnowledgeSearchError(RuntimeError):
    """受控运行中出现了**未选知识库**的检索请求。

    可信授权快照（本轮 ``expected``）明确列出了允许检索的 KB；任何不在该集合
    内的 KB 都不得触碰向量库/embedding。观察扩展在调用原生 ``search`` **之前**
    抛出本异常并记录失败，使该违规不会真正命中底层检索。
    """


@dataclass
class KnowledgeSearchObservation:
    """本轮运行对所选知识库真实检索的观察记录（可变、按轮独立）。"""

    expected: frozenset[str]
    succeeded: set[str] = field(default_factory=set)
    # kb id -> 失败原因（安全类型名，不含上游响应原文/凭据）。
    failures: dict[str, str] = field(default_factory=dict)

    def record_success(self, knowledge_base_id: str) -> None:
        self.succeeded.add(knowledge_base_id)

    def record_failure(self, knowledge_base_id: str, reason: str) -> None:
        # 同一 KB 多次检索时保留首次失败原因，后续成功不清除失败标记（本版本
        # static 每轮只检索一次，此处仅防御性去重）。
        self.failures.setdefault(knowledge_base_id, reason)

    def attempted(self) -> set[str]:
        """本轮真实尝试过检索的 KB id（成功或失败）。"""
        return self.succeeded | set(self.failures)

    def failed(self) -> dict[str, str]:
        """返回本轮真实检索失败的 ``{id: 原因}``（按 id 排序）。"""
        return {
            identifier: self.failures[identifier]
            for identifier in sorted(self.failures)
        }

    def missing(self) -> list[str]:
        """返回所选 KB 中**从未尝试真实检索**的 id（漏装/被跳过）。"""
        return sorted(self.expected - self.attempted())

    def unexpected(self) -> list[str]:
        """返回本轮检索中出现、但**不在所选集合内**的 KB id（配置漂移）。"""
        return sorted(self.attempted() - self.expected)

    def unsatisfied_reason(self) -> str | None:
        """所选 KB 全部成功真实检索且无未选 KB 参与时返回 ``None``。

        任一情况都返回安全中文原因：检索失败、所选 KB 漏装/被跳过，或出现未选
        KB 被检索（原生配置与可信快照漂移，绝不把未选 KB 用于检索）。
        """
        failed = self.failed()
        missing = self.missing()
        unexpected = self.unexpected()
        if not failed and not missing and not unexpected:
            return None
        parts: list[str] = []
        if failed:
            parts.append(
                "检索失败："
                + "、".join(
                    f"{identifier}（{reason}）"
                    for identifier, reason in failed.items()
                ),
            )
        if missing:
            parts.append("未发生真实检索：" + "、".join(missing))
        if unexpected:
            parts.append("出现未选知识库检索：" + "、".join(unexpected))
        return "；".join(parts)


_CURRENT: ContextVar[KnowledgeSearchObservation | None] = ContextVar(
    "nexus_knowledge_search_observation",
    default=None,
)


def set_observation(
    observation: KnowledgeSearchObservation,
) -> Token:
    """把本轮观察记录设为当前上下文；返回供 ``reset`` 的 token。"""
    return _CURRENT.set(observation)


def reset_observation(token: Token) -> None:
    _CURRENT.reset(token)


def current_observation() -> KnowledgeSearchObservation | None:
    return _CURRENT.get()


class ObservedKnowledgeBase(KnowledgeBase):
    """公开 ``KnowledgeBase`` 包装：只观察原生 ``search`` 成功/失败。

    暴露与原句柄相同的公开名称/描述/绑定，``search`` 完全委托原生句柄；仅
    在调用返回或抛异常时把结果写入当前观察记录，绝不改写检索结果、不额外的
    向量检索。
    """

    def __init__(
        self,
        inner: KnowledgeBase,
        knowledge_base_id: str,
    ) -> None:
        super().__init__(
            name=inner.name,
            description=inner.description,
            embedding_model=inner.embedding_model,
            vector_store=inner.vector_store,
            collection=inner.collection,
            metadata_filter=inner.metadata_filter,
        )
        self._inner = inner
        self.knowledge_base_id = knowledge_base_id

    async def search(
        self,
        queries: list,
        top_k: int = 5,
        score_threshold: float | None = None,
    ) -> list:
        observation = _CURRENT.get()
        # 受控运行（存在本轮观察）中，未选 KB 绝不允许真正检索：在调用原生
        # ``search`` 之前记录明确失败并拒绝，避免未选 KB 命中向量库/embedding。
        # 独立 REST 检索（无运行观察）仍走原生真正检索，不封死管理路径。
        if observation is not None and (
            self.knowledge_base_id not in observation.expected
        ):
            observation.record_failure(
                self.knowledge_base_id,
                "unexpected_knowledge_base",
            )
            raise UnexpectedKnowledgeSearchError(
                "未选知识库不允许在受控运行中检索。",
            )
        try:
            results = await self._inner.search(
                queries=queries,
                top_k=top_k,
                score_threshold=score_threshold,
            )
        except BaseException as exc:  # noqa: BLE001 - 如实记录后原样抛出
            if observation is not None:
                observation.record_failure(
                    self.knowledge_base_id,
                    type(exc).__name__,
                )
            raise
        if observation is not None:
            observation.record_success(self.knowledge_base_id)
        return results


class ObservedCollectionPerKbManager(CollectionPerKbManager):
    """公开 ``CollectionPerKbManager`` 子类：只包装 ``get_knowledge``。

    其余生命周期（创建/删除/列表、向量库进入退出）完全沿用官方实现，不重复
    进入向量库、不复制 manager 逻辑。
    """

    async def get_knowledge(
        self,
        user_id: str,
        knowledge_base_id: str,
    ) -> KnowledgeBase:
        inner = await super().get_knowledge(user_id, knowledge_base_id)
        return ObservedKnowledgeBase(inner, knowledge_base_id)
