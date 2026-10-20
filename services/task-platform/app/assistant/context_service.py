"""Agent Context 组织。

发送给 OpenCode 的上下文控制在必要范围：当前用户、当前页面、当前业务对象
ID、会话关联对象和平台时间。业务细节由 Agent 调 Tool 查询，不预先灌入。
页面上下文一律由后端按 ID 重新查询真实对象，不信任前端提交的数据。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.models import AssistantContextRef, AssistantConversation, AssistantMessage
from app.assistant.schemas import PageContext
from app.config.settings import get_settings
from app.models import AgentWorker, KnowledgeItem, RuntimeSession, Task

PAGE_TYPE_LABEL = {
    "task": "任务详情",
    "employee": "员工详情",
    "session": "执行会话",
    "knowledge": "数字图书馆",
    "library": "数字图书馆",
    "dashboard": "平台总览",
}

REF_LABEL = {"TASK": "任务", "EMPLOYEE": "员工", "SESSION": "执行会话", "KNOWLEDGE": "知识"}


class AssistantContextService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def resolve_page_context(
        self, page_context: PageContext | None
    ) -> tuple[str, list[tuple[str, str]]]:
        """按 ID 重新查询真实业务对象，返回（上下文文本，关联对象列表）。"""
        if not page_context or not page_context.type:
            return "", []
        entity_type = page_context.type
        entity_id = page_context.id
        if entity_type == "task" and entity_id:
            task = await self.session.get(Task, entity_id)
            if task:
                text = (
                    f"当前页面对象是任务 {task.id}《{task.title}》，"
                    f"状态 {task.status}，类型 {task.task_type}，优先级 {task.priority}，"
                    f"执行员工 {task.employee_id or '未分配'}，当前进展：{task.current_progress}。"
                )
                return text, [("TASK", task.id)]
        elif entity_type == "employee" and entity_id:
            employee = await self.session.get(AgentWorker, entity_id)
            if employee:
                text = (
                    f"当前页面对象是 Agent Worker {employee.id}（{employee.name}），"
                    f"岗位 {employee.role}，状态 {employee.status}。"
                )
                return text, [("EMPLOYEE", employee.id)]
        elif entity_type == "session" and entity_id:
            item = await self.session.get(RuntimeSession, entity_id)
            if item:
                text = (
                    f"当前页面对象是执行会话 {item.id}，属于任务 {item.task_id}，"
                    f"状态 {item.status}。"
                )
                return text, [("SESSION", item.id)]
        elif entity_type in {"knowledge", "library"}:
            label = PAGE_TYPE_LABEL.get(entity_type, "数字图书馆")
            if entity_id:
                item = await self.session.get(KnowledgeItem, entity_id)
                if item:
                    return (
                        f"当前页面是{label}中的知识《{item.title}》（{item.id}）。",
                        [("KNOWLEDGE", item.id)],
                    )
            return f"当前页面是{label}。", []
        elif entity_type == "dashboard":
            return "当前页面是平台总览。", []
        return "", []

    async def recent_refs(self, conversation: AssistantConversation, limit: int = 8) -> list[tuple[str, str]]:
        rows = (
            (
                await self.session.execute(
                    select(AssistantContextRef)
                    .where(AssistantContextRef.conversation_id == conversation.id)
                    .order_by(AssistantContextRef.created_at.desc(), AssistantContextRef.id.desc())
                    .limit(limit * 2)
                )
            )
            .scalars()
            .all()
        )
        seen: list[tuple[str, str]] = []
        for row in rows:
            key = (row.ref_type, row.ref_id)
            if key not in seen:
                seen.append(key)
            if len(seen) >= limit:
                break
        return seen

    async def build_context_block(
        self,
        conversation: AssistantConversation,
        page_context: PageContext | None,
    ) -> tuple[str, list[tuple[str, str]]]:
        settings = get_settings()
        local_time = datetime.now(ZoneInfo(settings.timezone))
        page_text, refs = await self.resolve_page_context(page_context)
        recent = await self.recent_refs(conversation)
        for ref in refs:
            if ref not in recent:
                recent.insert(0, ref)
        lines = [
            f"- 当前用户：{settings.assistant_user_id}",
            f"- 平台时间：{local_time:%Y-%m-%d %H:%M:%S}（{settings.timezone}）",
        ]
        if page_text:
            lines.append(f"- 当前页面：{page_text}")
        if recent:
            joined = "；".join(f"{REF_LABEL.get(kind, kind)} {ref_id}" for kind, ref_id in recent)
            lines.append(f"- 本会话最近涉及的对象：{joined}")
        block = "\n".join(lines)
        return block, refs

    async def history_summary(self, conversation: AssistantConversation, limit: int = 12) -> str:
        """OpenCode Session 丢失后用于恢复的最近对话摘要。"""
        rows = (
            (
                await self.session.execute(
                    select(AssistantMessage)
                    .where(
                        AssistantMessage.conversation_id == conversation.id,
                        AssistantMessage.role.in_(["USER", "ASSISTANT"]),
                        AssistantMessage.status.in_(["COMPLETED", "STREAMING"]),
                    )
                    .order_by(AssistantMessage.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return ""
        lines = []
        for message in reversed(rows):
            speaker = "用户" if message.role == "USER" else "助手"
            content = message.content.strip()
            if len(content) > 300:
                content = content[:300] + "…"
            if content:
                lines.append(f"{speaker}：{content}")
        return "\n".join(lines)
