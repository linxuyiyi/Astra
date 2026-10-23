from dataclasses import dataclass
from pathlib import PurePath

from app.domain import TaskType
from app.models import Task, TaskCategory


@dataclass(frozen=True, slots=True)
class TaskPromptContext:
    """Resolved category instructions for one Runtime execution."""

    category_id: str
    category_name: str
    prompt_kind: str
    prompt: str | None


_PROMPT_CONTEXT_ATTR = "_astra_task_prompt_context"


def resolve_task_prompt_context(task: Task, category: TaskCategory | None) -> TaskPromptContext | None:
    """Resolve the category prompt selected by task type without doing database I/O."""

    if category is None:
        return None
    if task.task_type == TaskType.ISSUE:
        prompt_kind = "question_prompt"
        raw_prompt = category.question_prompt
    elif task.task_type == TaskType.REQUIREMENT:
        prompt_kind = "task_prompt"
        raw_prompt = category.task_prompt
    else:
        return None
    prompt = raw_prompt if raw_prompt and raw_prompt.strip() else None
    return TaskPromptContext(
        category_id=category.id,
        category_name=category.name,
        prompt_kind=prompt_kind,
        prompt=prompt,
    )


def bind_task_prompt_context(task: Task, context: TaskPromptContext | None) -> None:
    """Attach ephemeral Runtime prompt context to the task instance used by all executors."""

    setattr(task, _PROMPT_CONTEXT_ATTR, context)


def get_task_prompt_context(task: Task) -> TaskPromptContext | None:
    context = getattr(task, _PROMPT_CONTEXT_ATTR, None)
    return context if isinstance(context, TaskPromptContext) else None


def build_task_prompt(
    task: Task,
    workspace: PurePath,
    *,
    retry: bool = False,
    native_question: bool = False,
    runtime_engine: str = "opencode",
) -> str:
    criteria = "\n".join(
        f"{item.get('id', f'AC-{index + 1:02d}')}. {item.get('content', '')}"
        for index, item in enumerate(task.acceptance_criteria or [])
    )
    human_input = (task.human_intervention or {}).get("response")
    suspension = task.suspension or {}
    retry_text = ""
    if retry:
        retry_text = "\n这是同一任务继续处理，请使用当前执行会话中的已有上下文。"
        if human_input:
            retry_text += f"\n人工补充信息：\n{human_input}"
    context_text = ""
    if task.current_progress:
        context_text += f"\n已保留的当前进展：\n{task.current_progress}"
    if suspension.get("scene_context"):
        context_text += f"\n挂起时现场上下文：\n{suspension['scene_context']}"
    if human_input and not retry:
        context_text += f"\n人工补充信息：\n{human_input}"

    prompt_context = get_task_prompt_context(task)
    task_type_label = {
        TaskType.ISSUE: "问题",
        TaskType.REQUIREMENT: "需求",
    }.get(task.task_type, str(task.task_type))
    category_text = ""
    if prompt_context:
        category_text = f"\n任务分类：\n{prompt_context.category_name}\n"
        if prompt_context.prompt:
            category_text += f"\n分类执行要求：\n{prompt_context.prompt}\n"

    attachment_rows = [
        f"- {item.get('name') or '附件'}：{item.get('workspace_path')}"
        for item in (task.attachments or [])
        if item.get("workspace_path")
    ]
    attachment_text = (
        "\n任务附件（已写入当前 Workspace，可直接读取）：\n" + "\n".join(attachment_rows)
        if attachment_rows
        else ""
    )

    intervention_instructions = """
当任务必须询问使用者、等待业务答案或需要操作授权时，必须使用 OpenCode 原生 question Tool
或 Permission；收到回答或授权后继续当前 Session。
不要用文本标记、自定义 JSON 或约定字符串伪装成人工阻塞状态。
"""

    if runtime_engine == "dsh":
        intervention_instructions = """
工具操作授权使用 ACP 原生权限申请。业务问题通过已登记的 Task MCP 请求人工介入，
等待真实业务答案后继续同一 Session；若工具尚未装配，应在最终回复中如实提出问题。
不要用文本标记、自定义 JSON 或约定字符串伪装成人工阻塞状态。
"""
    engine_label = "DSH" if runtime_engine == "dsh" else "OpenCode"
    return f"""当前任务：
{task.id}

任务来源：
{task.source}

任务类型：
{task_type_label}
{category_text}
任务目标：
{task.title}

任务描述：
{task.description}

员工工作空间：
{workspace}{attachment_text}

验收标准：
{criteria}

请在该工作空间内自主决定完成方式。Nexus 不规定你使用的 Agent、Skill、Tool、命令、
检查方式或内部步骤。分类执行要求用于补充该业务分类的处理规范，不替代当前任务目标、
任务描述或验收标准。Nexus 会直接依据 {engine_label} Session、Workspace 和最终回复获取执行事实，
不要输出 Nexus 专用 JSON 协议，也不要自行决定任务的系统状态。
{intervention_instructions}
完成任务后，请在最终回复中简洁说明：
1. 完成了什么；
2. 关键修改是什么；
3. 已完成哪些验证。
每轮回复后仍可在当前 Session 继续交流。只有使用者明确结束任务，Nexus 才关闭业务任务。
你只负责执行任务，不生成或提交经验，不调用任务复盘 Skill；
任务总结和经验沉淀由人工确认完成后的后台复盘统一处理。
无法完成或无法实测时如实说明；仅在需要使用者回答或授权才能继续时提出原生问题。
{context_text}{retry_text}
"""
