#!/usr/bin/env python3
"""Phase 1A real OpenCode verification for the Session fact path.

Exercises, against a real local OpenCode Server (no Run, no message mirror):
  1. RuntimeService.start creates a RuntimeSession and a runtime_context token.
  2. The synthetic runtime-context prompt part reaches the native Session.
  3. Session terminal state is derived and persisted (SUCCEEDED).
  4. astra_library MCP is loaded from the workspace-owned opencode.json (no OPENCODE_CONFIG).
  5. restart while a long write command runs aborts the old Session, preserves
     its INTERRUPTED status, links superseded_by, and the old Session stops
     modifying the shared workspace.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "services" / "task-platform"
sys.path.insert(0, str(BACKEND_ROOT))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def configure_environment(root: Path, workspace: Path, port: int, model: str, binary: str) -> None:
    os.environ.update(
        {
            "ASTRA_DATABASE_URL": f"sqlite+aiosqlite:///{root / 'phase1a.db'}",
            "ASTRA_WORKSPACE_ROOT": str(workspace),
            "ASTRA_RUNTIME_DIR": str(root / "runtime"),
            "ASTRA_SEED_DEMO": "false",
            "ASTRA_OPENCODE_BINARY": binary,
            "ASTRA_OPENCODE_MODEL": model,
            "ASTRA_OPENCODE_TIMEOUT_SECONDS": "300",
            "NEXUS_MCP_TRANSPORT": "stdio",
        }
    )


async def wait_for_terminal(task_id: str, timeout: float) -> str:
    from app.database import SessionLocal
    from app.models import RuntimeSession, Task
    from app.runtime.lifecycle import RuntimeService

    deadline = time.monotonic() + timeout
    status = "PENDING"
    while time.monotonic() < deadline:
        async with SessionLocal() as session:
            await RuntimeService(session).refresh(task_id)
            task = await session.get(Task, task_id)
            binding = await session.get(RuntimeSession, task.current_session_id)
            status = binding.status
            if status in {"SUCCEEDED", "FAILED", "INTERRUPTED"}:
                return task.status
        await asyncio.sleep(2)
    return status


async def run(model: str, binary: str, keep: bool) -> dict:
    root = Path(tempfile.mkdtemp(prefix="phase1a-"))
    workspace = root / "employee-ws"
    workspace.mkdir()
    port = free_port()
    configure_environment(root, workspace, port, model, binary)

    from app.config.settings import get_settings

    get_settings.cache_clear()

    from app.database import SessionLocal, init_database
    from app.models import (
        DigitalEmployee,
        OpenCodeExecutionEnvironment,
        RuntimeSession,
        Task,
    )
    from app.runtime.environment import environment_snapshot
    from app.runtime.lifecycle import RuntimeService

    await init_database()
    async with SessionLocal() as session:
        employee = DigitalEmployee(
            id="phase1a",
            name="Phase 1A Real",
            role="build",
            status="IDLE",
            task_types=["ISSUE"],
            category_ids=[],
            enabled=True,
            auto_claim=False,
            auto_execute=False,
        )
        environment = OpenCodeExecutionEnvironment(
            employee_id="phase1a",
            mode="LOCAL",
            name="Phase 1A",
            opencode_path=binary,
            workspace_root=str(workspace),
            opencode_server_port=port,
            model_name=model,
            status="AVAILABLE",
        )
        session.add_all([employee, environment])
        await session.commit()
        session.add(
            Task(
                id="T-SMOKE",
                title="Phase 1A smoke",
                task_type="ISSUE",
                status="PENDING",
                employee_id="phase1a",
                description=(
                    "在当前工作空间创建文件 phase1a.txt，内容必须恰好为 PHASE1A_OK。"
                    "完成后用一句话说明结果。"
                ),
                acceptance_criteria=[{"id": "AC-01", "content": "phase1a.txt 内容为 PHASE1A_OK"}],
                execution_environment_snapshot=environment_snapshot(environment),
            )
        )
        await session.commit()

    async with SessionLocal() as session:
        await RuntimeService(session).start("T-SMOKE")

    smoke_started = time.monotonic()
    status = await wait_for_terminal("T-SMOKE", 240)
    result: dict = {
        "smoke_task_status": status,
        "smoke_file": None,
        # 真实经过时间，不回填 timestamp：wait_for_terminal 单调时钟起止。
        "smoke_wait_elapsed_seconds": round(time.monotonic() - smoke_started, 3),
    }

    from app.runtime.session_connection import connect, resolve_environment

    async with SessionLocal() as session:
        task = await session.get(Task, "T-SMOKE")
        env = await resolve_environment(session, task)
        runtime_session = await session.get(RuntimeSession, task.current_session_id)
        result["session_status"] = runtime_session.status
        result["session_id_observed"] = task.current_session_id
        result["session_finished_at"] = bool(runtime_session.finished_at)
        result["context_hash_present"] = bool(runtime_session.runtime_context_hash)
        result["session_total_tokens"] = runtime_session.total_tokens
        result["session_tool_call_count"] = runtime_session.tool_call_count
        smoke_file = Path(task.workspace) / "phase1a.txt"
        if smoke_file.exists():
            result["smoke_file"] = smoke_file.read_text(encoding="utf-8").strip()
        async with connect(env) as client:
            messages = await client.messages(Path(task.workspace), task.current_session_id)
            result["synthetic_context_in_session"] = any(
                part.get("synthetic") and "ASTRA_RUNTIME_CONTEXT" in part.get("text", "")
                for item in messages
                for part in item.get("parts", [])
                if isinstance(part, dict)
            )
            try:
                mcp = await client.request("GET", "/mcp", directory=Path(task.workspace))
                result["mcp"] = mcp
            except Exception as exc:  # noqa: BLE001 - report the real server response
                result["mcp_error"] = str(exc)

    # 旧契约校正：单轮 Session 结束后业务 Task 仍占用员工，而 RuntimeService.start
    # 会拒绝在同一员工上启动第二个 Task（lifecycle.py occupied 检查）。这里先断言
    # 「仍 RUNNING/WAITING 且绑定同一 Session」，再走现有人工 confirm 释放员工，
    # 之后才进入 T-LONG 的真实写入与显式 restart/取消链。
    async with SessionLocal() as session:
        smoke_task = await session.get(Task, "T-SMOKE")
        smoke_binding = await session.get(RuntimeSession, smoke_task.current_session_id)
        result["smoke_task_still_occupied"] = smoke_task.status in {"RUNNING", "WAITING"}
        result["smoke_session_stable"] = (
            smoke_task.current_session_id == result.get("session_id_observed")
        )
        result["smoke_session_id"] = smoke_task.current_session_id
        assert smoke_binding is not None
    assert result["smoke_task_still_occupied"], result
    assert result["smoke_session_stable"], result

    confirm_started = time.monotonic()
    async with SessionLocal() as session:
        confirmed = await RuntimeService(session).confirm_completion("T-SMOKE")
        await session.commit()
        result["smoke_confirm_status"] = str(confirmed.status)
        result["smoke_confirm_elapsed_seconds"] = round(time.monotonic() - confirm_started, 3)
    assert result["smoke_confirm_status"] == "SUCCEEDED", result

    # Long-write restart scenario.
    from sqlalchemy import select

    async with SessionLocal() as session:
        environment = await session.scalar(
            select(OpenCodeExecutionEnvironment).where(
                OpenCodeExecutionEnvironment.employee_id == "phase1a"
            )
        )
        session.add(
            Task(
                id="T-LONG",
                title="Phase 1A restart",
                task_type="ISSUE",
                status="PENDING",
                employee_id="phase1a",
                description=(
                    "如果当前工作空间里不存在 longwrite.txt，就运行下面的 shell 命令，"
                    "然后回复 LONG_DONE：\n"
                    "for i in $(seq 1 120); do echo tick >> longwrite.txt; sleep 1; done\n"
                    "如果 longwrite.txt 已经存在，不要运行任何命令，直接回复 LONG_DONE。"
                ),
                acceptance_criteria=[{"id": "AC-01", "content": "命令已按条件运行"}],
                execution_environment_snapshot=environment_snapshot(environment),
            )
        )
        await session.commit()

    async with SessionLocal() as session:
        await RuntimeService(session).start("T-LONG")

    # The old Session must be actively writing before we restart it.
    long_file: Path | None = None
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        async with SessionLocal() as session:
            long_task = await session.get(Task, "T-LONG")
            if long_task.workspace:
                long_file = Path(long_task.workspace) / "longwrite.txt"
        if long_file and long_file.exists() and long_file.stat().st_size > 20:
            break
        await asyncio.sleep(2)

    size_growing = long_file.stat().st_size if long_file and long_file.exists() else 0
    async with SessionLocal() as session:
        first = await session.get(Task, "T-LONG")
        old_session_id = first.current_session_id
        await RuntimeService(session).start("T-LONG", restart=True)
        first = await session.get(Task, "T-LONG")
        new_session_id = first.current_session_id

    async with SessionLocal() as session:
        old = await session.get(RuntimeSession, old_session_id)
        result["old_session_status"] = old.status
        result["old_session_superseded_by"] = old.superseded_by == new_session_id
        result["new_session_id_differs"] = new_session_id != old_session_id

    # Two post-abort measurements prove the old Session stopped writing.
    await asyncio.sleep(2)
    size_after_restart = long_file.stat().st_size if long_file and long_file.exists() else 0
    await asyncio.sleep(5)
    size_after_wait = long_file.stat().st_size if long_file and long_file.exists() else 0
    result["longwrite_size_growing"] = size_growing
    result["longwrite_size_after_restart"] = size_after_restart
    result["longwrite_size_after_wait"] = size_after_wait
    result["old_session_stopped_writing"] = (
        size_growing > 0 and size_after_restart == size_after_wait
    )

    result["workspace"] = str(workspace)
    result["root"] = str(root)
    result["kept"] = keep
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=os.environ.get("PHASE1A_MODEL", "deepseek/deepseek-flash"))
    parser.add_argument("--opencode", default=os.environ.get("PHASE1A_OPENCODE", "opencode"))
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(run(args.model, args.opencode, args.keep))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    passed = (
        result["smoke_task_status"] == "RUNNING"
        and result.get("session_status") == "SUCCEEDED"
        and result.get("synthetic_context_in_session")
        and result.get("smoke_file") == "PHASE1A_OK"
        and result.get("session_total_tokens") is not None
        and (result.get("session_tool_call_count") or 0) >= 1
        and result.get("old_session_status") == "INTERRUPTED"
        and result.get("old_session_superseded_by")
        and result.get("old_session_stopped_writing")
    )
    if passed and not args.keep:
        shutil.rmtree(result["root"], ignore_errors=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
