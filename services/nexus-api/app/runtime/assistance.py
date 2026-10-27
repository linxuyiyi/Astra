"""责任员工通过当前运行时绑定处理求助；不保存原生问题的副本。"""

from pathlib import Path
from urllib.parse import quote

from app.models import RuntimeSession
from app.runtime.dsh_lifecycle import DshLifecycle
from app.runtime.dsh_projection import project_event
from app.runtime.experience_review.trace_normalizer import normalize_messages
from app.runtime.lifecycle import RuntimeStateError, _cancel_record
from app.runtime.redaction import redact_data
from app.runtime.session_connection import connect, resolve_runtime_session_environment, session_tree
from app.services import TaskService


class WorkerAssistance:
    def __init__(self, runtime, *, employee_id, source_session_id):
        self.runtime = runtime
        self.db = runtime.session
        self.employee_id = employee_id
        self.source_session_id = source_session_id

    async def context(self, task_id, expected_session_id=None):
        task = await self.runtime.lock_task(task_id)
        if (
            not self.employee_id or not self.source_session_id
            or task.responsible_employee_id != self.employee_id
            or task.source_session_id != self.source_session_id
        ):
            raise RuntimeStateError("任务不属于当前员工及来源对话")
        if task.status not in {"RUNNING", "WAITING"}:
            raise RuntimeStateError("当前任务不能处理任务执行端的求助")
        if expected_session_id and task.current_session_id != expected_session_id:
            raise RuntimeStateError("当前执行会话已切换，请重新查询求助")
        if _cancel_record(task) or task.pending_message_id:
            raise RuntimeStateError("任务正在停止或输入尚未确认，请稍后重新查询")
        binding = await self.db.get(RuntimeSession, task.current_session_id, populate_existing=True)
        if (
            not binding or binding.task_id != task.id or binding.superseded_by
            or binding.employee_id != task.employee_id
        ):
            raise RuntimeStateError("当前运行时绑定无效")
        return task, binding

    async def read(self, task_id, *, cursor=0):
        task, binding = await self.context(task_id)
        session_id, anchor = binding.id, task.last_message_id
        engine, native_id = binding.runtime_engine, binding.native_session_id
        environment = await resolve_runtime_session_environment(self.db, binding)
        await self.db.commit()
        output, next_cursor = [], None
        if engine == "dsh":
            client = await DshLifecycle(self.runtime).client(binding)
            await self.db.commit()
            snapshot = await client.snapshot()
            requests = [{**item, "kind": "permission"} for item in snapshot.get("permissions", [])]
            can_input = (
                snapshot.get("state") == "succeeded" and snapshot.get("stop_reason") == "end_turn"
                and snapshot.get("submission_id") == anchor and not requests
            )
            if can_input:
                page = await client.read_trace_page(cursor=max(0, cursor), limit=40)
                output = [
                    value for event in page["events"]
                    if (value := project_event(event, native_id)) is not None
                ]
                next_cursor = page.get("next_cursor")
        else:
            async with connect(environment, start=True) as client:
                directory = Path(binding.workspace)
                allowed = await session_tree(client, directory, session_id)
                requests = [
                    {**item, "kind": kind}
                    for kind, items in (
                        ("question", await client.questions(directory)),
                        ("permission", await client.permissions(directory)),
                    )
                    for item in items if item.get("sessionID") in allowed
                ]
                messages = await self.runtime._all_messages(client, directory, session_id)
                messages.sort(key=lambda item: item.get("info", {}).get("id", ""))
                events = [
                    event.model_dump(mode="json")
                    for event in normalize_messages(messages, session_id)
                    if event.role == "assistant" or event.kind == "error"
                ]
                offset = max(0, cursor)
                output = events[offset:offset + 40]
                next_cursor = offset + len(output) if offset + len(output) < len(events) else None
            can_input = False
        # 查询期间切换、停止或进入新回合，不能返回可执行的旧请求。
        current, _ = await self.context(task_id, session_id)
        if current.last_message_id != anchor:
            raise RuntimeStateError("当前回合已变化，请重新查询求助")
        await self.db.commit()
        return redact_data({
            "ok": True, "task_id": task_id, "session_id": session_id,
            "runtime_engine": engine, "submission_id": anchor, "requests": requests,
            "output": output, "next_cursor": next_cursor, "can_input": can_input,
        })

    async def reply(
        self, task_id, *, expected_session_id, request_id, kind,
        answers=None, decision=None,
    ):
        task, binding = await self.context(task_id, expected_session_id)
        session_id, fence, native_id = binding.id, binding.fencing_token, binding.native_session_id

        async def revalidate():
            current, active = await self.context(task_id, session_id)
            if active.fencing_token != fence or active.native_session_id != native_id:
                raise RuntimeStateError("当前执行轮次已变化，请重新查询求助")
            return current, active

        # 慢查询不持有行锁；仅原生回复发送到受理期间串行化停止/切换。
        if binding.runtime_engine == "dsh":
            if kind != "permission":
                raise RuntimeStateError("DSH 业务问题请通过 nexus_task_input 续答")
            client = await DshLifecycle(self.runtime).client(binding)
            await self.db.commit()
            snapshot = await client.snapshot()
            pending = next((x for x in snapshot.get("permissions", []) if x["id"] == request_id), None)
            if not pending or pending.get("native_session_id") != binding.native_session_id:
                raise RuntimeStateError("审批已过期或不属于当前执行会话")
            option = next((x for x in pending["options"] if x["optionId"] == decision), None)
            if not option or option["kind"] not in {"allow_once", "reject_once"}:
                raise RuntimeStateError("请选择请求提供的本次允许或本次拒绝选项")
            task, binding = await revalidate()
            await client.reply_permission(permission_id=request_id, option_id=decision)
        else:
            if kind not in {"question", "permission"}:
                raise RuntimeStateError("不支持的求助类型")
            environment = await resolve_runtime_session_environment(self.db, binding)
            await self.db.commit()
            async with connect(environment, start=True) as client:
                directory = Path(binding.workspace)
                allowed = await session_tree(client, directory, binding.id)
                items = await client.request("GET", f"/{kind}", directory=directory)
                pending = next((
                    x for x in items if x["id"] == request_id and x.get("sessionID") in allowed
                ), None)
                if not pending:
                    raise RuntimeStateError("请求已过期或不属于当前任务的执行会话")
                if kind == "question":
                    questions = pending.get("questions") or []
                    if (
                        not answers or len(answers) != len(questions)
                        or any(not row or any(not x.strip() for x in row) for row in answers)
                    ):
                        raise RuntimeStateError("必须按问题顺序提供每个问题的答案")
                    body = {"answers": answers}
                else:
                    if decision not in {"once", "reject"}:
                        raise RuntimeStateError("权限仅支持本次允许或本次拒绝")
                    body = {"reply": decision}
                task, binding = await revalidate()
                result = await client.request(
                    "POST", f"/{kind}/{quote(request_id, safe='')}/reply", directory=directory,
                    body=body, session_id=binding.id, task_id=task.id,
                )
                if result is False:
                    raise RuntimeStateError("任务执行端未受理回复，请重新查询")
        # 原生受理之后，审计或刷新失败都不能诱使模型重发已消费的回复。
        try:
            await TaskService(self.db).add_event(
                task, "WORKER_ASSISTANCE_REPLIED", "work", "责任员工已回复任务执行端的求助",
                {"session_id": binding.id, "request_id": request_id, "kind": kind,
                 "responsible_employee_id": task.responsible_employee_id},
            )
            await self.runtime.refresh(task_id)
        except Exception:
            await self.db.rollback()
            return {"ok": True, "accepted": True, "task_id": task_id, "refresh_required": True}
        return {"ok": True, "accepted": True, "task_id": task_id, "refresh_required": False}

    async def input(self, task_id, *, expected_session_id, expected_submission_id, text):
        task, binding = await self.context(task_id, expected_session_id)
        if binding.runtime_engine != "dsh" or not expected_submission_id:
            raise RuntimeStateError("仅支持按当前 DSH 回合续答")
        if not text.strip() or len(text) > 100000:
            raise RuntimeStateError("续答不能为空或超过 100000 字符")
        if task.last_message_id != expected_submission_id:
            raise RuntimeStateError("当前回合已变化，请重新查询求助")
        return await DshLifecycle(self.runtime).send(
            task_id, text, expected_binding_id=binding.id,
            expected_submission_id=expected_submission_id,
            expected_source=(self.employee_id, self.source_session_id),
        )
