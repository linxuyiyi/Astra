from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import EmployeeStatus, EnvironmentStatus, TaskStatus
from app.models import (
    AgentWorker,
    Event,
    OpenCodeExecutionEnvironment,
    RuntimeSession,
    Task,
)
from app.runtime.environment import environment_snapshot


async def seed_demo_data(session: AsyncSession) -> None:
    if await session.scalar(select(func.count(AgentWorker.id))):
        await upgrade_demo_v03(session)
        return

    now = datetime.now(UTC)
    employees = [
        AgentWorker(
            id="astra-01",
            name="Astra-01",
            role="测试平台开发",
            description="负责测试平台问题和需求处理",
            default_work_area="development",
            status=EmployeeStatus.WORKING,
            task_types=["ISSUE", "REQUIREMENT"],
            category_ids=["testing-platform", "automation-engineering"],
            enabled=True,
            auto_claim=True,
            auto_execute=True,
            current_task_count=1,
            online_since=now - timedelta(hours=8),
            last_heartbeat=now,
        ),
        AgentWorker(
            id="astra-02",
            name="Astra-02",
            role="自动化工程师",
            description="负责自动化工程建设与维护",
            default_work_area="integration",
            status=EmployeeStatus.WAIT_HUMAN,
            task_types=["ISSUE", "REQUIREMENT"],
            category_ids=["automation-engineering"],
            enabled=True,
            auto_claim=True,
            auto_execute=True,
            current_task_count=1,
            online_since=now - timedelta(hours=6),
            last_heartbeat=now,
        ),
        AgentWorker(
            id="astra-03",
            name="Astra-03",
            role="防线运营专员",
            description="负责质量防线运行与治理",
            default_work_area="special",
            status=EmployeeStatus.IDLE,
            task_types=["ISSUE", "REQUIREMENT"],
            category_ids=["quality-defense"],
            enabled=True,
            auto_claim=True,
            auto_execute=True,
            current_task_count=0,
            online_since=now - timedelta(hours=5),
            last_heartbeat=now,
        ),
    ]
    session.add_all(employees)
    await session.flush()
    environments = [
        OpenCodeExecutionEnvironment(
            employee_id="astra-01",
            mode="LOCAL",
            name="本地开发工作空间",
            description="测试平台工程",
            opencode_path="opencode",
            model_name="deepseek/deepseek-v4-flash",
            workspace_root="./workspaces/astra-01",
            status=EnvironmentStatus.AVAILABLE,
            status_detail={"message": "演示环境"},
            last_checked_at=now - timedelta(minutes=10),
        ),
        OpenCodeExecutionEnvironment(
            employee_id="astra-02",
            mode="LINUX_REMOTE",
            name="自动化开发机",
            description="Linux 远程工作空间",
            host="10.20.8.42",
            username="astra02",
            opencode_path="/usr/local/bin/opencode",
            workspace_root="/home/astra02/automation",
            status=EnvironmentStatus.AVAILABLE,
            status_detail={"message": "演示环境"},
            last_checked_at=now - timedelta(minutes=16),
        ),
        OpenCodeExecutionEnvironment(
            employee_id="astra-03",
            mode="LINUX_REMOTE",
            name="防线运营机",
            description="Linux 远程工作空间",
            host="10.20.8.43",
            username="astra03",
            opencode_path="/usr/local/bin/opencode",
            workspace_root="/home/astra03/quality-defense",
            status=EnvironmentStatus.AVAILABLE,
            status_detail={"message": "演示环境"},
            last_checked_at=now - timedelta(minutes=22),
        ),
    ]
    session.add_all(environments)
    await session.flush()
    snapshots = {item.employee_id: environment_snapshot(item) for item in environments}

    criteria_login = [
        {"id": "AC-01", "content": "当前 500 问题解决"},
        {"id": "AC-02", "content": "原有登录能力正常"},
        {"id": "AC-03", "content": "相关检查通过"},
        {"id": "AC-04", "content": "提供修改结果说明"},
    ]
    tasks = [
        Task(
            id="T-20260827-001",
            source="manual",
            task_type="ISSUE",
            category_id="testing-platform",
            title="修复用户登录接口偶发 500 问题",
            description="用户登录接口在特定情况下偶发 500，请定位并解决。",
            acceptance_criteria=criteria_login,
            priority="VERY_HIGH",
            priority_weight=4,
            completion_policy="MANUAL_CONFIRM",
            status=TaskStatus.RUNNING,
            employee_id="astra-01",
            current_progress="已定位权限缓存问题，正在检查影响范围",
            execution_environment_snapshot=snapshots["astra-01"],
            created_at=now - timedelta(minutes=34),
            updated_at=now - timedelta(minutes=8),
            claimed_at=now - timedelta(minutes=31),
            started_at=now - timedelta(minutes=30),
            last_activity_at=now - timedelta(minutes=8),
        ),
        Task(
            id="T-20260827-002",
            source="manual",
            task_type="REQUIREMENT",
            category_id="automation-engineering",
            title="新增回调场景自动化覆盖",
            description="补充支付回调异常和幂等场景的自动化覆盖。",
            acceptance_criteria=[
                {"id": "AC-01", "content": "关键回调场景完成覆盖"},
                {"id": "AC-02", "content": "提供执行结果说明"},
            ],
            priority="HIGH",
            priority_weight=3,
            completion_policy="MANUAL_CONFIRM",
            status=TaskStatus.WAITING,
            employee_id="astra-02",
            current_progress="已完成场景设计，等待测试环境访问权限",
            human_required_reason="当前账号没有修改测试环境配置的权限",
            human_intervention={
                "blocking_reason": "当前账号没有修改测试环境配置的权限",
                "completed_work": "已完成场景设计并定位需要的环境配置",
                "current_state": "等待环境权限",
                "requested_input": "请提供可操作该环境的账号，或完成对应配置",
            },
            execution_environment_snapshot=snapshots["astra-02"],
            created_at=now - timedelta(minutes=60),
            updated_at=now - timedelta(minutes=47),
            claimed_at=now - timedelta(minutes=58),
            started_at=now - timedelta(minutes=56),
            last_activity_at=now - timedelta(minutes=47),
        ),
        Task(
            id="T-20260827-003",
            source="nr_test_system",
            external_task_id="NR-5021",
            task_type="REQUIREMENT",
            category_id="quality-defense",
            title="完善接口变更防线告警信息",
            description="增强告警上下文，便于快速定位变更来源。",
            acceptance_criteria=[
                {"id": "AC-01", "content": "告警包含变更接口与来源信息"},
                {"id": "AC-02", "content": "现有防线运行正常"},
            ],
            priority="MEDIUM",
            priority_weight=2,
            completion_policy="AUTO",
            status=TaskStatus.SUCCEEDED,
            employee_id="astra-03",
            current_progress="任务已完成",
            execution_environment_snapshot=snapshots["astra-03"],
            created_at=now - timedelta(hours=2, minutes=10),
            updated_at=now - timedelta(hours=1, minutes=24),
            claimed_at=now - timedelta(hours=2),
            started_at=now - timedelta(hours=2),
            finished_at=now - timedelta(hours=1, minutes=24),
            last_activity_at=now - timedelta(hours=1, minutes=24),
        ),
        Task(
            id="T-20260827-004",
            source="manual",
            task_type="ISSUE",
            category_id="testing-platform",
            title="排查任务列表时间筛选异常",
            description="任务列表在跨天时间范围下出现结果缺失，请定位并解决。",
            acceptance_criteria=[
                {"id": "AC-01", "content": "跨天时间范围返回完整结果"},
                {"id": "AC-02", "content": "原有筛选能力保持正常"},
            ],
            priority="LOW",
            priority_weight=1,
            completion_policy="AUTO",
            status=TaskStatus.PENDING,
            current_progress="等待领取",
            created_at=now - timedelta(minutes=20),
            updated_at=now - timedelta(minutes=20),
            last_activity_at=now - timedelta(minutes=20),
        ),
        Task(
            id="T-20260827-005",
            source="manual",
            task_type="REQUIREMENT",
            category_id="quality-defense",
            title="优化防线异常通知内容",
            description="Agent Worker 已提交结果，等待管理员确认。",
            acceptance_criteria=[{"id": "AC-01", "content": "通知内容清晰可定位"}],
            priority="HIGH",
            priority_weight=3,
            completion_policy="MANUAL_CONFIRM",
            status=TaskStatus.WAITING,
            employee_id="astra-03",
            current_progress="结果已提交，等待确认",
            created_at=now - timedelta(minutes=20),
            updated_at=now - timedelta(minutes=12),
            submitted_at=now - timedelta(minutes=12),
            last_activity_at=now - timedelta(minutes=12),
        ),
        Task(
            id="T-20260827-006",
            source="manual",
            task_type="ISSUE",
            category_id="automation-engineering",
            title="等待外部测试数据后继续排查",
            description="当前可安全释放员工，待数据到齐后继续。",
            acceptance_criteria=[{"id": "AC-01", "content": "完成问题定位"}],
            priority="MEDIUM",
            priority_weight=2,
            completion_policy="AUTO",
            status=TaskStatus.WAITING,
            employee_id="astra-02",
            current_progress="已保留工作现场，等待外部数据",
            suspension={
                "reason": "外部测试数据尚未生成",
                "scene_context": "已排除本地配置问题",
                "suspended_at": (now - timedelta(minutes=25)).isoformat(),
            },
            created_at=now - timedelta(minutes=40),
            updated_at=now - timedelta(minutes=25),
            started_at=now - timedelta(minutes=36),
            last_activity_at=now - timedelta(minutes=25),
        ),
    ]
    session.add_all(tasks)
    await session.flush()

    session.add_all(
        [
            RuntimeSession(
                id="session-task-001",
                task_id="T-20260827-001",
                employee_id="astra-01",
                workspace=snapshots["astra-01"]["workspace_root"],
                environment=snapshots["astra-01"],
                status="RUNNING",
                started_at=now - timedelta(minutes=30),
                last_activity_at=now - timedelta(minutes=30),
            ),
            RuntimeSession(
                id="session-task-002",
                task_id="T-20260827-002",
                employee_id="astra-02",
                workspace=snapshots["astra-02"]["workspace_root"],
                environment=snapshots["astra-02"],
                status="WAITING",
                started_at=now - timedelta(minutes=47),
            ),
            RuntimeSession(
                id="session-task-003",
                task_id="T-20260827-003",
                employee_id="astra-03",
                workspace=snapshots["astra-03"]["workspace_root"],
                environment=snapshots["astra-03"],
                status="SUCCEEDED",
                started_at=now - timedelta(hours=2),
                finished_at=now - timedelta(hours=1, minutes=24),
                model_name="deepseek/deepseek-v4-flash",
                input_tokens=42000,
                output_tokens=6800,
                cache_read_tokens=112000,
                cache_write_tokens=0,
                total_tokens=160800,
                llm_call_count=7,
                tool_call_count=32,
                tool_usage=[
                    {"tool_name": "bash", "call_count": 12, "success_count": 11,
                     "failure_count": 1, "duration_ms": 18400},
                    {"tool_name": "read", "call_count": 11, "success_count": 11,
                     "failure_count": 0, "duration_ms": 3200},
                    {"tool_name": "edit", "call_count": 6, "success_count": 6,
                     "failure_count": 0, "duration_ms": 5100},
                    {"tool_name": "skill", "call_count": 3, "success_count": 3,
                     "failure_count": 0, "duration_ms": 2100},
                ],
            ),
        ]
    )
    await session.flush()
    for task_id, session_id in (
        ("T-20260827-001", "session-task-001"),
        ("T-20260827-002", "session-task-002"),
        ("T-20260827-003", "session-task-003"),
    ):
        task_row = await session.get(Task, task_id)
        task_row.current_session_id = session_id
    for employee_id in ("astra-01", "astra-02"):
        employee_row = await session.get(AgentWorker, employee_id)
        employee_row.current_task_count = 1
    session.add_all(
        [
            Event(
                task_id=task_id,
                employee_id=employee_id,
                event_type=event_type,
                phase="work",
                message=message,
                detail={},
                created_at=now + timedelta(minutes=offset),
            )
            for task_id, employee_id, event_type, message, offset in (
                ("T-20260827-001", None, "TASK_CREATED", "任务已创建", -34),
                ("T-20260827-001", "astra-01", "TASK_ASSIGNED", "任务已分配给 Astra-01", -32),
                (
                    "T-20260827-001",
                    "astra-01",
                    "WORK_PROGRESS",
                    "已定位权限缓存问题，正在检查影响范围",
                    -8,
                ),
                ("T-20260827-002", "astra-02", "WAIT_HUMAN", "Astra-02 请求人工协助", -47),
                ("T-20260827-003", "astra-03", "TASK_COMPLETED", "Astra-03 完成任务", -84),
            )
        ]
    )
    await session.commit()


async def upgrade_demo_v03(session: AsyncSession) -> None:
    employees = (await session.execute(select(AgentWorker))).scalars().all()
    for employee in employees:
        employee.task_types = employee.task_types or ["ISSUE", "REQUIREMENT"]
        employee.category_ids = employee.category_ids or [
            "testing-platform",
            "automation-engineering",
            "quality-defense",
        ]
    tasks = (await session.execute(select(Task))).scalars().all()
    for task in tasks:
        task.completion_policy = task.completion_policy or (
            "MANUAL_CONFIRM" if task.priority in {"VERY_HIGH", "HIGH"} else "AUTO"
        )
        task.last_activity_at = task.last_activity_at or task.updated_at
    await session.commit()
