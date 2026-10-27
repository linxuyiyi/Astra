from pathlib import PurePath

from app.domain import TaskType
from app.executors.opencode_prompt import (
    bind_task_prompt_context,
    build_task_prompt,
    resolve_task_prompt_context,
)
from app.models import Task, TaskCategory


def make_task(task_type: TaskType) -> Task:
    return Task(
        id=f"TASK-{task_type.value}",
        source="manual",
        task_type=task_type,
        category_id="CAT-001",
        title="验证分类提示词",
        description="确认 Runtime 会使用任务分类配置的提示词。",
        acceptance_criteria=[{"id": "AC-01", "content": "分类提示词进入实际执行 Prompt"}],
    )


def make_category() -> TaskCategory:
    return TaskCategory(
        id="CAT-001",
        name="自动化工程",
        task_prompt="REQUIREMENT_CATEGORY_PROMPT_MARKER",
        question_prompt="ISSUE_CATEGORY_PROMPT_MARKER",
        enabled=True,
    )


def test_issue_runtime_prompt_uses_question_prompt_only():
    task = make_task(TaskType.ISSUE)
    context = resolve_task_prompt_context(task, make_category())
    bind_task_prompt_context(task, context)

    prompt = build_task_prompt(task, PurePath("/workspace"))

    assert context is not None
    assert context.prompt_kind == "question_prompt"
    assert "任务类型：\n问题" in prompt
    assert "任务分类：\n自动化工程" in prompt
    assert "ISSUE_CATEGORY_PROMPT_MARKER" in prompt
    assert "REQUIREMENT_CATEGORY_PROMPT_MARKER" not in prompt
    assert "ASTRA_RESULT_JSON:" not in prompt
    assert "ASTRA_INTERVENTION_JSON:" not in prompt


def test_requirement_runtime_prompt_uses_task_prompt_only():
    task = make_task(TaskType.REQUIREMENT)
    context = resolve_task_prompt_context(task, make_category())
    bind_task_prompt_context(task, context)

    prompt = build_task_prompt(task, PurePath("/workspace"), native_question=True)

    assert context is not None
    assert context.prompt_kind == "task_prompt"
    assert "任务类型：\n需求" in prompt
    assert "REQUIREMENT_CATEGORY_PROMPT_MARKER" in prompt
    assert "ISSUE_CATEGORY_PROMPT_MARKER" not in prompt
    assert "OpenCode 原生提问工具" in prompt
    assert "不要输出 Nexus 专用 JSON 协议" in prompt
    assert "完成任务后，请在最终回复中简洁说明" in prompt
    assert "ASTRA_RESULT_JSON:" not in prompt
    assert "ASTRA_INTERVENTION_JSON:" not in prompt


def test_blank_category_prompt_degrades_to_generic_runtime_prompt():
    task = make_task(TaskType.ISSUE)
    category = make_category()
    category.question_prompt = "  \n  "
    context = resolve_task_prompt_context(task, category)
    bind_task_prompt_context(task, context)

    prompt = build_task_prompt(task, PurePath("/workspace"))

    assert context is not None
    assert context.prompt is None
    assert "任务分类：\n自动化工程" in prompt
    assert "分类执行要求：" not in prompt
    assert "ASTRA_RESULT_JSON:" not in prompt
    assert "ASTRA_INTERVENTION_JSON:" not in prompt


def test_runtime_prompt_lists_materialized_task_attachments():
    task = make_task(TaskType.ISSUE)
    task.attachments = [
        {
            "name": "requirements.md",
            "workspace_path": "/workspace/uploads/abc-requirements.md",
        }
    ]

    prompt = build_task_prompt(task, PurePath("/workspace"))

    assert "任务附件（已写入当前 Workspace，可直接读取）" in prompt
    assert "requirements.md：/workspace/uploads/abc-requirements.md" in prompt
