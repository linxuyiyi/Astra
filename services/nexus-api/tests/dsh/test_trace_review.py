from types import SimpleNamespace

import pytest

from app.runtime.experience_review import dsh_trace
from app.runtime.experience_review.evidence import validate_candidate
from app.runtime.experience_review.schemas import Candidate
from app.runtime.experience_review.trace_reader import TraceUnavailable
from app.worker_agent.dsh_acp.skills import frozen_manifest, manifest, snapshot


@pytest.fixture
def source(monkeypatch):
    binding = SimpleNamespace(id="binding", task_id="task", native_session_id="native-uuid")
    frozen = {
        "root_session_id": "binding",
        "task_id": "task",
        "native_session_id": "native-uuid",
        "source_session_ids": ["binding"],
        "native_trace_sha256": "a" * 64,
        "native_event_count": 2,
    }
    events = [
        {
            "seq": 0,
            "time": 1792770000000,
            "type": "tool/call",
            "data": {"callId": "call", "name": "bash", "arguments": "test 1 = 1"},
        },
        {
            "seq": 1,
            "time": 1792770000001,
            "type": "tool/result",
            "data": {
                "message": {
                    "id": "msg",
                    "toolCallId": "call",
                    "content": [{"type": "text", "text": "PASS"}],
                }
            },
        },
    ]
    pages = [
        dict(
            session_id="native-uuid",
            complete=True,
            sha256="a" * 64,
            event_count=2,
            events=[event],
            next_cursor=index + 1 if index == 0 else None,
        )
        for index, event in enumerate(events)
    ]

    class Client:
        async def read_trace_page(self, *, cursor, limit):
            return pages[cursor]

    monkeypatch.setattr(dsh_trace, "DshRuntimeClient", lambda *_: Client())
    return binding, SimpleNamespace(id=1), frozen, pages


async def test_native_pages_reuse_review_and_candidate_evidence_contract(source):
    binding, environment, frozen, _ = source
    result = await dsh_trace.read_dsh_trace(binding, environment, frozen)
    assert len(result.events) == 2 and result.sessions == [binding.native_session_id]
    assert result.message_count == 1  # tool/call 不是另一条原生消息。
    assert all(event.source_system == "dsh" for event in result.events)
    validate_candidate(
        Candidate(
            title="验证命令结果", content_md="仅引用真实原生工具结果。", evidence_refs=[result.events[1].ref]
        ),
        result,
    )
    assert result.events[1].part_id == "call" and result.events[1].message_id == "msg"


@pytest.mark.parametrize("change", ["hash", "incomplete", "duplicate", "native"])
async def test_changed_incomplete_duplicate_or_cross_session_evidence_is_rejected(source, change):
    binding, environment, frozen, pages = source
    if change == "hash":
        pages[1]["sha256"] = "b" * 64
    elif change == "incomplete":
        pages[1]["complete"] = False
    elif change == "duplicate":
        pages[1]["events"] = pages[0]["events"]
    else:
        frozen["native_session_id"] = "other-session"
    with pytest.raises(TraceUnavailable):
        await dsh_trace.read_dsh_trace(binding, environment, frozen)


def test_skill_resources_are_frozen_and_source_updates_do_not_rewrite_session(tmp_path):
    source = tmp_path / "review"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: review\ndescription: Review changes\n---\nVersion one")
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    before = snapshot({"review": source}, home)
    (source / "SKILL.md").write_text("---\nname: review\ndescription: Review changes\n---\nVersion two")
    assert frozen_manifest(home) == before
    assert manifest({"review": source}) != before
    (home / "skills" / "review" / "link").symlink_to(source / "SKILL.md")
    with pytest.raises(ValueError, match="符号链接"):
        frozen_manifest(home)
