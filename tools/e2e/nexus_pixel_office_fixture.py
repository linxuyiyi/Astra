"""创建独立的办公室视觉验收库，使用真实 API 投影，不启动执行器。

用法：services/nexus-api/.venv/bin/python tools/e2e/nexus_pixel_office_fixture.py
       --directory /tmp/nexus-office-qa-<unique> --employees 3
目标文件必须不存在；不读取、修改当前运行数据库，不包含模型凭据。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "services/nexus-api"))



def create_directory_database(directory: Path, count: int) -> Path:
    from app.sql_storage import connect, create_test_schema
    from nexus_api.avatars import CATALOG
    from nexus_api.db_models import employees

    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "office.db"
    if database.exists():
        raise SystemExit(f"拒绝覆盖已有数据库：{database}")
    create_test_schema(database)
    names = ["小牛马", "小鲁班", "小蒜鸟", "小橘子", "小青柠", "小满格"]
    roles = ["测试自动化工程师", "测试系统工程师", "防线运营人员"]
    appearances = [f"robot-v1-{i}" for i in (0, 2, 4, 1, 3, 5)] + [
        item["id"] for item in CATALOG["items"] if item["family"] == "pixel"
    ]
    with connect(database) as connection:
        for index in range(count):
            role = roles[index % 3]
            connection.execute(
                employees.insert().values(
                    id=f"office-e{index}",
                    name=names[index] if index < len(names) else f"同事{index + 1:02}",
                    domain=role,
                    description=role,
                    instructions="办公室验收夹具，不调用模型",
                    avatar=appearances[index % len(appearances)],
                    enabled=1,
                    knowledge_json="[]",
                    skills_json="[]",
                    mcp_json="[]",
                    tools_json="[]",
                    agentscope_agent_id=f"office-agent-{index}",
                    mailbox_session_id="",
                    capability_json="{}",
                    model_route_id="",
                    created_at=f"2026-10-29T00:00:{index:02}+00:00",
                    updated_at="2026-10-29T00:00:00+00:00",
                )
            )
        connection.commit()
    return database


async def seed_work(database: Path) -> None:
    from app.models import AgentWorker, OpenCodeExecutionEnvironment, Task

    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    async with sessions() as session:
        # 第一位员工有 23 个资源，覆盖分页、共享、等待、异常、执行、空闲。
        for index in range(23):
            worker = AgentWorker(
                id=f"office-w{index:02}",
                name=f"Worker-{index + 1:02}",
                role="测试执行",
                enabled=True,
                status="IDLE",
                task_types=["ISSUE"],
                category_ids=[],
                digital_employee_id=None if index in (0, 1) else "office-e0",
            )
            session.add(worker)
            await session.flush()
            session.add(
                OpenCodeExecutionEnvironment(
                    employee_id=worker.id,
                    mode="LOCAL",
                    name=f"测试环境 {index + 1}",
                    worker_channel="ssh",
                    status="UNAVAILABLE" if index == 2 else "AVAILABLE",
                    labels=[],
                )
            )
        for index, (status, title, worker, reason) in enumerate(
            [
                ("WAITING", "测试环境访问权限确认", "office-w00", "HITL_PERMISSION"),
                ("RUNNING", "登录接口自动化回归", "office-w01", None),
                ("RUNNING", "测试平台服务健康检查", "office-w03", None),
                ("FAILED", "接口兼容性验证", None, None),
                ("PENDING", "新增规格用例补充", None, None),
            ]
        ):
            session.add(
                Task(
                    id=f"office-task-{index}",
                    task_type="ISSUE",
                    title=title,
                    status=status,
                    responsible_employee_id="office-e0",
                    employee_id=worker,
                    priority="MEDIUM",
                    runtime_connection="UNAVAILABLE" if index == 2 else "CONNECTED",
                    waiting_reason=reason,
                    human_required_reason="请确认是否授权访问预发布测试环境"
                    if index == 0
                    else None,
                    failure_reason="测试数据连接被拒绝，请检查环境配置"
                    if status == "FAILED"
                    else None,
                    current_progress="验证核心业务接口" if status == "RUNNING" else "",
                )
            )
        for index in range(24):
            session.add(
                Task(
                    id=f"office-pending-{index}",
                    task_type="ISSUE",
                    title=f"回归用例补充 {index + 1}",
                    status="PENDING",
                    responsible_employee_id="office-e0",
                    priority="MEDIUM",
                )
            )
        session.add(
            Task(
                id="office-other",
                task_type="ISSUE",
                title="测试系统构建验证",
                status="RUNNING",
                responsible_employee_id="office-e1",
                employee_id="office-w01",
                priority="MEDIUM",
            )
        )
        session.add(
            Task(
                id="office-done",
                task_type="ISSUE",
                title="防线巡检已完成",
                status="SUCCEEDED",
                responsible_employee_id="office-e2",
                priority="MEDIUM",
                completed_at=now,
            )
        )
        await session.commit()
    await engine.dispose()


def create_preview_app():
    """复用正式路由与聊天初始化，省略平台执行器/调度器 lifespan。"""
    from nexus_api.agentscope_proxy import router as agentscope_proxy_router
    from nexus_api.main import create_app
    from nexus_api.service import _include_platform, _mount_frontend

    app = create_app()
    _include_platform(app)
    app.include_router(agentscope_proxy_router)
    _mount_frontend(app)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--employees", type=int, choices=[0, 3, 6, 12, 16], default=3)
    args = parser.parse_args()
    path = create_directory_database(args.directory.resolve(), args.employees)
    if args.employees:
        asyncio.run(seed_work(path))
    print(f"已创建独立验收数据库：{path}")
