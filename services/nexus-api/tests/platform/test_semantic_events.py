from app.runtime.semantic_events import (
    adapt_open_code_event,
    adapt_open_code_events,
    extract_agent_text,
    extract_inquiry,
)


def test_usage_and_session_events_become_hidden_runtime_metadata():
    usage = adapt_open_code_event(
        {
            "type": "step_finish",
            "part": {"tokens": {"input": 12, "output": 3}},
        }
    )
    session = adapt_open_code_event({"type": "result", "session_id": "ses-private"})

    assert usage and usage.event_type == "runtime.metadata"
    assert session and session.event_type == "runtime.metadata"


def test_agent_text_and_tool_events_have_stable_semantics():
    message = adapt_open_code_event({"type": "text", "part": {"text": "我先检查任务上下文。"}})
    tool = adapt_open_code_event(
        {
            "type": "tool",
            "part": {
                "tool": "read",
                "state": {
                    "status": "completed",
                    "title": "读取任务说明",
                    "input": {"filePath": "README.md"},
                },
            },
        }
    )

    assert message and message.event_type == "message.agent"
    assert extract_agent_text({"type": "text", "part": {"text": "最终输出"}}) == "最终输出"
    assert tool and tool.event_type == "tool.completed"
    assert tool.payload["tool"] == "read"
    assert tool.payload["input"] == {"filePath": "README.md"}


def test_real_inquiry_and_subagent_lifecycle_are_preserved():
    inquiry_event = {
        "type": "question",
        "part": {
            "question": "请告诉我需要打印的具体答案是什么？",
            "description": "任务要求使用用户提供的答案",
            "options": [{"label": "示例答案", "value": "示例答案"}],
            "status": "waiting",
        },
    }
    inquiry = extract_inquiry(inquiry_event)
    subagent = adapt_open_code_event(
        {
            "type": "subagent",
            "part": {"name": "research-agent", "status": "completed", "role": "资料检查"},
        }
    )

    assert inquiry == {
        "blocking_reason": "任务要求使用用户提供的答案",
        "completed_work": "已保留当前执行上下文",
        "current_state": "等待用户回答",
        "requested_input": "请告诉我需要打印的具体答案是什么？",
        "options": [{"label": "示例答案", "value": "示例答案"}],
    }
    assert subagent and subagent.event_type == "subagent.completed"
    assert subagent.agent_name == "research-agent"


def test_opencode_task_tool_projects_tool_and_subagent_from_structured_fields():
    raw_event = {
        "type": "tool_use",
        "timestamp": 1788089251026,
        "sessionID": "ses_parent",
        "part": {
            "id": "prt_task",
            "messageID": "msg_parent",
            "sessionID": "ses_parent",
            "type": "tool",
            "tool": "task",
            "callID": "call_task_1",
            "state": {
                "status": "completed",
                "input": {
                    "description": "Print hello world",
                    "prompt": "打印 hello world",
                    "subagent_type": "general",
                },
                "output": (
                    '<task id="ses_child" state="completed"><task_result>hello world</task_result></task>'
                ),
                "metadata": {
                    "parentSessionId": "ses_parent",
                    "sessionId": "ses_child",
                    "model": {"modelID": "deepseek-v4-flash", "providerID": "deepseek"},
                },
                "title": "Print hello world",
                "time": {"start": 1788089249950, "end": 1788089251023},
            },
        },
    }

    events = adapt_open_code_events(raw_event)

    assert [item.event_type for item in events] == ["tool.completed", "subagent.completed"]
    assert events[0].payload["tool"] == "task"
    assert events[0].payload["call_id"] == "call_task_1"
    assert events[1].agent_name == "general"
    assert events[1].payload["source_call_id"] == "call_task_1"
    assert events[1].payload["parent_session_id"] == "ses_parent"
    assert events[1].payload["child_session_id"] == "ses_child"
    assert events[1].payload["model"] == {
        "modelID": "deepseek-v4-flash",
        "providerID": "deepseek",
    }


def test_assistant_prose_never_fabricates_a_subagent_event():
    events = adapt_open_code_events(
        {
            "type": "text",
            "part": {"type": "text", "text": "新的 subagent 返回了 hello world"},
        }
    )

    assert [item.event_type for item in events] == ["message.agent"]


def test_reasoning_and_unknown_events_are_diagnostic_without_reasoning_content():
    reasoning = adapt_open_code_events(
        {"type": "reasoning", "part": {"type": "reasoning", "text": "private chain"}}
    )[0]
    unknown = adapt_open_code_events({"type": "future_runtime_signal", "value": 1})[0]

    assert reasoning.event_type == "runtime.reasoning"
    assert "private chain" not in reasoning.summary
    assert reasoning.payload["content_stored"] is False
    assert unknown.event_type == "runtime.unknown"


def test_official_part_shapes_cover_subtask_artifact_and_retry_semantics():
    subtask = adapt_open_code_events(
        {
            "type": "message.part.updated",
            "part": {
                "type": "subtask",
                "agent": "explore",
                "description": "Inspect repository",
                "prompt": "Find the implementation",
            },
        }
    )[0]
    patch = adapt_open_code_events(
        {"type": "message.part.updated", "part": {"type": "patch", "files": ["app.py"]}}
    )[0]
    file_event = adapt_open_code_events(
        {
            "type": "message.part.updated",
            "part": {"type": "file", "filename": "report.html", "mime": "text/html"},
        }
    )[0]
    retry = adapt_open_code_events({"type": "message.part.updated", "part": {"type": "retry", "attempt": 2}})[
        0
    ]

    assert (subtask.event_type, subtask.agent_name) == ("subagent.started", "explore")
    assert patch.event_type == "artifact.updated"
    assert patch.payload["files"] == ["app.py"]
    assert file_event.event_type == "artifact.created"
    assert retry.event_type == "runtime.retry"


def test_server_question_and_message_error_shapes_are_native_semantics():
    inquiry = adapt_open_code_events(
        {
            "type": "question.asked",
            "properties": {
                "id": "que_1",
                "sessionID": "ses_main",
                "questions": [
                    {
                        "question": "Choose a marker",
                        "options": [{"label": "BLUE", "description": "Blue marker"}],
                    }
                ],
            },
        }
    )[0]
    failure = adapt_open_code_events(
        {
            "type": "message.updated",
            "info": {
                "sessionID": "ses_main",
                "error": {"name": "MessageAbortedError", "data": {"message": "Aborted"}},
            },
        }
    )[0]

    assert inquiry.event_type == "inquiry.requested"
    assert inquiry.summary == "Choose a marker"
    assert inquiry.payload["runtime_request_id"] == "que_1"
    assert inquiry.payload["options"] == [{"label": "BLUE", "description": "Blue marker"}]
    assert failure.event_type == "runtime.error"
    assert failure.summary == "Aborted"
