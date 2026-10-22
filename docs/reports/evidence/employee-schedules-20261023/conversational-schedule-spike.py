"""对话创建日程的归属与工具重定向验证（设计验证脚本，不是产品代码）。

在临时 SQLite 上使用 AgentScope 2.0.10 官方 ScheduleCreate / ScheduleList，
验证三件事：

1. 官方工具可以写入调用方指定的人类日程命名空间，并保留 source=AGENT、
   source_session_id 与会话模型快照。
2. 沿用会话的 X-User-ID（Nexus 当前为平台用户 nexus）直接创建时，记录不会
   出现在人类命名空间，且会进入平台用户的列表。
3. 按“私有会话才保留、按会话所属人类重定向、日程执行会话移除”的规则，
   工具归属决策符合预期。

运行方式（使用安装 agentscope==2.0.10 的虚拟环境）：

    <venv>/bin/python conversational-schedule-spike.py > conversational-schedule-spike.json

脚本只写 /tmp 下的临时 SQLite 文件，不连接 Nexus、Redis 或真实 AgentScope 服务。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

from agentscope.app._manager._scheduler._scheduler_manager import SchedulerManager
from agentscope.app._manager._scheduler._tools import ScheduleCreate, ScheduleList
from agentscope.app.storage import AsyncSQLAlchemyStorage
from agentscope.app.storage._model import ChatModelConfig
from agentscope.state import AgentState

DB = "/tmp/nexus-schedule-conversational-spike.db"
HUMAN_UID = "schedule-qa-alice"
HUMAN_NS = "nexus-schedule-" + hashlib.sha256(HUMAN_UID.encode()).hexdigest()
OTHER_NS = "nexus-schedule-" + hashlib.sha256(b"schedule-qa-bob").hexdigest()
PLATFORM_NS = "nexus"
AGENT_ID = "schedule-preview-agent"
SESSION_ID = "chat-session-alice-1"
MODEL = ChatModelConfig(
    type="openai_credential",
    credential_id="schedule-preview-credential",
    model="test",
    parameters={},
)


class FakeSchedulerManager:
    def __init__(self) -> None:
        self.notified: list[str] = []

    @staticmethod
    def validate_schedule(record):
        return SchedulerManager.validate_schedule(record)

    async def notify_changed(self, schedule_id: str) -> None:
        self.notified.append(schedule_id)


class FakeScheduler:
    def get_jobs(self):
        return []


class FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self._user_id = PLATFORM_NS


class FakeGroup:
    def __init__(self, tools):
        self.name = "schedule_tools"
        self.tools = tools


class FakeToolkit:
    def __init__(self):
        self.tool_groups = [FakeGroup([FakeTool(name) for name in (
            "ScheduleCreate", "ScheduleList", "ScheduleView", "ScheduleDelete",
        )])]


def text_of(chunk) -> str:
    return "\n".join(getattr(block, "text", "") for block in chunk.content)


def proposed_owner_for(session_kind: str, origin_type: str, lookup_ok: bool = True) -> str | None:
    """拟议规则：只有私有会话、非日程执行会话、且归属查询成功才保留工具。"""

    if session_kind != "private" or origin_type == "schedule" or not lookup_ok:
        return None
    return HUMAN_NS


def retarget(toolkit: FakeToolkit, owner: str | None) -> list[str]:
    kept: list[str] = []
    for group in toolkit.tool_groups:
        group.tools = [tool for tool in group.tools if owner is not None]
        for tool in group.tools:
            tool._user_id = owner
            kept.append(tool.name)
    return kept


async def main() -> dict:
    for suffix in ("", "-shm", "-wal"):
        try:
            os.unlink(DB + suffix)
        except FileNotFoundError:
            pass

    result: dict = {
        "title": "对话创建日程：官方工具归属与重定向验证",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "agentscope": "2.0.10",
            "storage": "sqlite+aiosqlite (temporary)",
            "scheduler": "fake manager; official validate_schedule/notify contract",
            "nexus_services": "not connected",
        },
        "identities": {
            "human_uid": HUMAN_UID,
            "human_namespace": HUMAN_NS,
            "other_namespace": OTHER_NS,
            "platform_namespace": PLATFORM_NS,
        },
        "checks": {},
        "limitations": [
            "未经过真实聊天模型工具调用；只验证官方工具、存储归属和字段。",
            "未经过 Nexus HTTP 适配和页面；页面读取同一人类命名空间。",
            "未验证定时器墙钟触发、MySQL 8.0.36、生产 Nginx 或多副本。",
        ],
    }

    storage = AsyncSQLAlchemyStorage("sqlite+aiosqlite:///" + DB, create_tables=True)
    async with storage:
        manager = FakeSchedulerManager()

        # 1. 官方工具写入人类命名空间。
        create = ScheduleCreate(HUMAN_NS, AGENT_ID, MODEL, storage, manager)
        chunk = await create(
            name="工作日晨间简报",
            description="每个工作日 9 点整理当天工作重点，生成简报并列出下一步。",
            cron_expression="0 9 * * 1-5",
            timezone="Asia/Shanghai",
            _agent_state=AgentState(session_id=SESSION_ID),
        )
        record = (await storage.list_schedules(HUMAN_NS))[0]
        result["checks"].update({
            "create_returns_success": "created successfully" in text_of(chunk),
            "owner_is_human_namespace": record.user_id == HUMAN_NS,
            "agent_is_current_employee": record.agent_id == AGENT_ID,
            "source_is_agent": record.data.source.value == "AGENT",
            "source_session_recorded": record.data.source_session_id == SESSION_ID,
            "model_snapshot_recorded": record.data.chat_model_config.model == "test",
            "timezone_preserved": record.data.timezone == "Asia/Shanghai",
            "scheduler_notified": manager.notified == [record.id],
        })
        result["record"] = {
            "id": record.id,
            "user_id": record.user_id,
            "agent_id": record.agent_id,
            "name": record.data.name,
            "cron": record.data.cron_expression,
            "timezone": record.data.timezone,
            "source": record.data.source.value,
            "source_session_id": record.data.source_session_id,
            "model": record.data.chat_model_config.model,
        }

        # 2. 列表按命名空间隔离。
        human_list = text_of(await ScheduleList(HUMAN_NS, FakeScheduler(), storage)())
        other_list = text_of(await ScheduleList(OTHER_NS, FakeScheduler(), storage)())
        result["checks"].update({
            "human_list_sees_it": "工作日晨间简报" in human_list,
            "other_human_does_not": "工作日晨间简报" not in other_list,
        })

        # 3. 直接沿用平台身份：人类页面不可见，平台列表可见。
        naive = ScheduleCreate(PLATFORM_NS, AGENT_ID, MODEL, storage, manager)
        await naive(
            name="平台归属日程",
            description="模拟直接放开官方工具而不做归属重定向。",
            cron_expression="0 10 * * *",
            timezone="Asia/Shanghai",
            _agent_state=AgentState(session_id=SESSION_ID),
        )
        human_after = await storage.list_schedules(HUMAN_NS)
        platform_after = await storage.list_schedules(PLATFORM_NS)
        result["checks"].update({
            "naive_platform_owned_is_invisible_to_human": all(
                item.data.name != "平台归属日程" for item in human_after
            ),
            "naive_platform_owned_leaks_into_platform_list": any(
                item.data.name == "平台归属日程" for item in platform_after
            ),
        })
        result["platform_list_after_naive"] = [
            {"name": item.data.name, "agent_id": item.agent_id} for item in platform_after
        ]

        # 4. 推荐的重建路径：官方 SchedulerManager.list_tools() 直接按人类命名空间构造。
        official_manager = SchedulerManager(
            storage=storage,
            message_bus=object(),
            workspace_manager=object(),
            enabled=False,
        )
        rebuilt = await official_manager.list_tools(
            user_id=HUMAN_NS,
            agent_id=AGENT_ID,
            chat_model_config=MODEL,
        )
        result["checks"].update({
            "official_list_tools_returns_four": len(rebuilt) == 4,
            "rebuilt_owner_is_human": all(tool._user_id == HUMAN_NS for tool in rebuilt),
            "rebuilt_names_match": {tool.name for tool in rebuilt} == {
                "ScheduleCreate", "ScheduleList", "ScheduleView", "ScheduleDelete",
            },
        })

        # 5. 拟议的会话类型/来源决策。
        decisions = {}
        for label, kind, origin, lookup_ok in (
            ("private_user_session", "private", "user", True),
            ("team_member_session", "team", "team", True),
            ("mailbox_session", "mailbox", "user", True),
            ("schedule_execution_session", "private", "schedule", True),
            ("owner_lookup_unavailable", "private", "user", False),
        ):
            owner = proposed_owner_for(kind, origin, lookup_ok)
            tools = retarget(FakeToolkit(), owner)
            decisions[label] = {
                "owner": owner,
                "schedule_create_kept": "ScheduleCreate" in tools,
                "schedule_tools": tools,
            }
        result["tool_decision"] = decisions
        result["checks"].update({
            "private_session_keeps_create": decisions["private_user_session"]["schedule_create_kept"],
            "team_session_drops_create": not decisions["team_member_session"]["schedule_create_kept"],
            "mailbox_session_drops_create": not decisions["mailbox_session"]["schedule_create_kept"],
            "schedule_session_drops_create": not decisions["schedule_execution_session"]["schedule_create_kept"],
            "lookup_failure_fails_closed": not decisions["owner_lookup_unavailable"]["schedule_create_kept"],
            "retarget_owner_is_human": decisions["private_user_session"]["owner"] == HUMAN_NS,
        })

    result["conclusion"] = (
        "官方 ScheduleCreate 可以在不改 AgentScope 的前提下写入人类命名空间，并保留 "
        "source=AGENT、source_session_id、模型快照和时区；但工具默认沿用会话的 X-User-ID。"
        "Nexus 所有聊天都在平台用户 nexus 下，直接放开会把日程写到平台名下：人类页面看不到，"
        "平台列表还会跨人可见。拟议的最小路线是只在私有、非日程执行的会话保留官方工具，"
        "把四个工具的 _user_id 重定向到当前人类的 nexus-schedule-<sha256(uid)>，查询失败即移除。"
    )
    return result


if __name__ == "__main__":
    json.dump(asyncio.run(main()), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
