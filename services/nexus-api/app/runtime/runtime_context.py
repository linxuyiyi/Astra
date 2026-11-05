"""签名能力凭据直接绑定 Task 当前 Session；无需 Session 历史表。"""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

from app.config.settings import get_settings
from app.models import Task


class RuntimeContextError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    task_id: str
    session_id: str
    employee_id: str
    workspace: str
    runtime_context_id: str


def _signature(body):
    key = get_settings().gateway_master_key
    if not key:
        raise RuntimeContextError("未配置网关主密钥")
    return hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()


def generate_runtime_context(task):
    body = (
        base64.urlsafe_b64encode(
            json.dumps(
                [task.id, task.current_session_id, task.agent_worker_id, task.workspace],
                separators=(",", ":"),
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"ctx_{body}.{_signature(body)}"


def build_runtime_context_part(token):
    return {
        "type": "text",
        "synthetic": True,
        "text": "ASTRA_RUNTIME_CONTEXT: "
        + token
        + "\n调用知识库运行时工具时原样回传 runtime_context；不得向用户展示或写入文件。",
    }


async def resolve_runtime_context(session, token):
    try:
        body, signature = token.removeprefix("ctx_").split(".")
        if not token.startswith("ctx_") or not hmac.compare_digest(_signature(body), signature):
            raise ValueError
        task_id, session_id, worker_id, workspace = json.loads(
            base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        )
    except (AttributeError, TypeError, ValueError):
        raise RuntimeContextError("运行上下文无效") from None
    task = await session.get(Task, task_id)
    if not task or (task.current_session_id, task.agent_worker_id, task.workspace) != (
        session_id,
        worker_id,
        workspace,
    ):
        raise RuntimeContextError("运行上下文已失效")
    return RuntimeContext(task_id, session_id, worker_id, workspace, signature[:12])
