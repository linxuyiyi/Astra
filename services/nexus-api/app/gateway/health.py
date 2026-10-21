"""探测结果落库与健康状态推进的共享逻辑。

控制面手动探测与数据面后台探测都使用同一套阈值与状态推进规则，避免同一业务
规则在两处重复实现：

- 探测结果写入 ``gateway_health_checks``（只保存耗时、状态码、错误分类与元数据）。
- 节点健康投影（``gateway_nodes`` 的 health_state / 连击 / health_detail）与运行态
  （Redis）使用相同阈值推进，使管理端 DB 展示与调度实际读取的运行态一致。

阈值直接取自运行态实现，保证两侧收敛。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.gateway.models import (
    HEALTH_HEALTHY,
    HEALTH_UNHEALTHY,
    PROBE_TYPES,
    GatewayNode,
    GatewayProbeRun,
    GatewayRuntimeEvent,
)
from app.gateway.probe import (
    ProbeOutcome,
    probe_types_for_strategy,
    strategy_for_outcomes,
)
from app.gateway.runtime import (
    PROBE_FAILURE_THRESHOLD,
    PROBE_SUCCESS_THRESHOLD,
    node_runtime_scope,
)
from app.models import utcnow


def probe_steps(outcomes: list[ProbeOutcome]) -> list[dict[str, Any]]:
    """轮次内的步骤事实：只保留耗時、状态码、错误分类与元数据。"""

    return [
        {
            "probe_type": outcome.probe_type,
            "success": outcome.success,
            "latency_ms": outcome.latency_ms,
            "status_code": outcome.status_code,
            "error": outcome.error,
            "detail": outcome.detail,
        }
        for outcome in outcomes
    ]


def build_probe_run(
    node: GatewayNode,
    outcomes: list[ProbeOutcome],
    *,
    source: str,
    started_at,
    duration_ms: int,
    strategy: str | None = None,
) -> GatewayProbeRun:
    """一次探测操作 = 一条轮次记录；步骤在 steps 里，不再逐步骤落行。"""

    strategy = strategy or strategy_for_outcomes(outcomes)
    complete = probe_complete(outcomes, probe_types_for_strategy(strategy))
    success = probe_success(outcomes)
    return GatewayProbeRun(
        id="gpr_" + uuid4().hex,
        node_id=node.id,
        source=source,
        strategy=strategy,
        first_model=(node.models or [None])[0],
        runtime_scope=node_runtime_scope(node),
        success=success,
        complete=complete,
        status="SUCCESS" if success and complete else "PARTIAL" if complete else "INCOMPLETE",
        steps=probe_steps(outcomes),
        usage={"requests": len(outcomes)},
        error="" if success else "；".join(
            f"{outcome.probe_type}: {outcome.error}" for outcome in outcomes if not outcome.success
        )[:2000],
        started_at=started_at,
        finished_at=utcnow(),
        duration_ms=duration_ms,
    )


def probe_success(outcomes: list[ProbeOutcome]) -> bool:
    """一组探测全部成功才算成功；空结果按失败处理。"""

    return bool(outcomes) and all(outcome.success for outcome in outcomes)


def probe_complete(
    outcomes: list[ProbeOutcome], required: list[str] | None = None
) -> bool:
    """是否覆盖该策略要求的完整步骤。

    常规验证是一次 SSE（STREAM）；专项验证是一次非流式（MODEL）；SCOPED 只用于
    诊断，要求四类步骤齐全，因此不会用局部成功把 UNKNOWN 晋升为 HEALTHY。
    """

    expected = set(required if required is not None else PROBE_TYPES)
    return bool(outcomes) and expected.issubset({outcome.probe_type for outcome in outcomes})


def apply_probe_outcome(node: GatewayNode, *, success: bool) -> None:
    """按与运行态一致的阈值推进节点健康投影与连击。"""

    if success:
        node.success_streak += 1
        node.failure_streak = 0
        if node.success_streak >= PROBE_SUCCESS_THRESHOLD:
            node.health_state = HEALTH_HEALTHY
    else:
        node.failure_streak += 1
        node.success_streak = 0
        if node.failure_streak >= PROBE_FAILURE_THRESHOLD:
            node.health_state = HEALTH_UNHEALTHY
    node.last_checked_at = utcnow()


def record_health_transition(session: AsyncSession, node: GatewayNode, previous: str) -> None:
    """健康态真正变化时写一条运行事件。连击未达阈值时不记。"""

    if node.health_state == previous:
        return
    session.add(
        GatewayRuntimeEvent(
            source="runtime",
            kind="health",
            node_id=node.id,
            pool_id=node.pool_id,
            summary=f"{node.name} 健康态 {previous} → {node.health_state}",
            previous=previous,
            current=node.health_state,
            detail={
                "success_streak": node.success_streak,
                "failure_streak": node.failure_streak,
            },
        )
    )


def probe_detail(node: GatewayNode, outcomes: list[ProbeOutcome]) -> dict[str, Any]:
    return {
        "checked": len(outcomes),
        "failed": sum(1 for outcome in outcomes if not outcome.success),
        "success_streak": node.success_streak,
        "failure_streak": node.failure_streak,
    }


async def record_background_probe(
    session_factory: async_sessionmaker[AsyncSession],
    node_id: str,
    outcomes: list[ProbeOutcome],
    *,
    expected_scope: str | None = None,
    strategy: str | None = None,
) -> None:
    """数据面后台探测落库。

    写入 ``gateway_health_checks``，并把 ``gateway_nodes`` 的健康投影推进到与运行态
    相同的状态，使管理端 DB 展示不会落后于调度读取的运行态。节点已不在当前配置
    时跳过（例如配置已删除）。调用方负责容错，落库失败不得影响数据面转发。

    ``expected_scope`` 是本次探测对应的「探测连接指纹」：只有当保存连接的指纹
    与它一致时才更新当前健康投影，否则（保存内容已改成别的连接）旧连接探测结果不得把
    新连接判为 HEALTHY，健康记录仍保留为历史证据。
    """

    success = probe_success(outcomes)
    async with session_factory() as session:
        node = await session.get(GatewayNode, node_id)
        if node is None:
            return
        started_at = utcnow()
        run = build_probe_run(
            node,
            outcomes,
            source="AUTO",
            started_at=started_at,
            duration_ms=0,
            strategy=strategy,
        )
        session.add(run)
        if expected_scope is not None and node_runtime_scope(node) != expected_scope:
            await session.commit()
            return
        previous_health = node.health_state
        apply_probe_outcome(node, success=success)
        record_health_transition(session, node, previous_health)
        node.health_detail = probe_detail(node, outcomes)
        await session.commit()