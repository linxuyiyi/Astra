"""对已启动的 Redis、AgentScope 2.0.10 和 Nexus API 做任务 6.1。

未设置 NEXUS_LIVE_AGENTSCOPE=1 时跳过。服务没起来时失败，而不是改成模拟回复。

    NEXUS_LIVE_AGENTSCOPE=1 pytest services/nexus-api/tests/test_live_agentscope.py -s
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NEXUS_LIVE_AGENTSCOPE") != "1",
    reason="设置 NEXUS_LIVE_AGENTSCOPE=1，并先按 services/agent-service/agentscope.md 启动服务",
)

NEXUS = os.environ.get("NEXUS_API_BASE", "http://127.0.0.1:18120").rstrip("/")
AGENTSCOPE = os.environ.get("NEXUS_AGENTSCOPE_BASE_URL", "http://127.0.0.1:18000").rstrip("/")
USER_ID = os.environ.get("NEXUS_AGENTSCOPE_USER_ID", "nexus")
OWNER = {"X-Owner-Id": "live-6-1"}
AGENT_HEADERS = {"X-User-ID": USER_ID}


def _timeout(read: float) -> httpx.Timeout:
    return httpx.Timeout(connect=10, read=read, write=30, pool=10)


def collect_events(
    url: str,
    headers: dict,
    params: dict | None,
    read: float,
    ignore_ids: set[str] | None = None,
) -> list[dict]:
    """读到一条新的 REPLY_END。ignore_ids 用来跳过同一条流里已经看过的重放。"""
    ignored = ignore_ids or set()
    events: list[dict] = []
    with httpx.Client(timeout=_timeout(read)) as client:
        with client.stream("GET", url, headers=headers, params=params) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload = json.loads(line[len("data:") :].strip())
                event_id = payload.get("id")
                if isinstance(event_id, str) and event_id in ignored:
                    continue
                events.append(payload)
                if isinstance(event_id, str):
                    ignored.add(event_id)
                if payload.get("type") in {"REPLY_END", "NEXUS_UPSTREAM_ERROR"}:
                    break
    return events


def deltas(events: list[dict]) -> str:
    return "".join(
        item.get("delta", "")
        for item in events
        if item.get("type") == "TEXT_BLOCK_DELTA" and isinstance(item.get("delta"), str)
    )


def tool_names(events: list[dict]) -> list[str]:
    names: list[str] = []
    for item in events:
        if item.get("type") in {"TOOL_CALL_START", "TOOL_RESULT_START"}:
            name = item.get("tool_call_name")
            if isinstance(name, str):
                names.append(name)
    return names


def tool_fragments(events: list[dict]) -> str:
    parts: list[str] = []
    for item in events:
        if item.get("type") in {"TOOL_CALL_DELTA", "TOOL_RESULT_TEXT_DELTA"}:
            delta = item.get("delta")
            if isinstance(delta, str):
                parts.append(delta)
    return "".join(parts)


def test_task_6_1_agent_sessions_sse_and_peer_wakeup():
    suffix = uuid.uuid4().hex[:6]
    star_name = f"星{suffix}"
    peer_name = f"知{suffix}"
    with httpx.Client(timeout=_timeout(30)) as client:
        health = client.get(f"{AGENTSCOPE}/health", headers=AGENT_HEADERS)
        assert health.status_code == 200, health.text
        runtime = client.get(f"{NEXUS}/api/v1/runtime")
        assert runtime.status_code == 200, runtime.text
        assert runtime.json()["reachable"] is True
        assert runtime.json()["agentscope_version"] == "2.0.10"

        star = _create_employee(
            client,
            name=star_name,
            domain=f"产品{suffix}",
            instructions=(
                "用户要求联系同事时，必须调用 contact_employee。"
                "employee_name 与 message 使用用户给出的原文。"
                "不要代替对方写出答复。"
            ),
        )
        peer = _create_employee(
            client,
            name=peer_name,
            domain=f"运营{suffix}",
            instructions="收到同事提示后，按提示原文作答，不要扩写。",
        )
        assert star["agentscope"]["ok"] is True, star
        assert peer["agentscope"]["ok"] is True, peer
        star_employee = star["employee"]
        peer_employee = peer["employee"]
        agents = client.get(f"{AGENTSCOPE}/agent/", headers=AGENT_HEADERS)
        assert agents.status_code == 200, agents.text
        agent_ids = {item["id"] for item in agents.json()["agents"]}
        assert star_employee["agentscope_agent_id"] in agent_ids
        assert peer_employee["agentscope_agent_id"] in agent_ids

        renamed = client.patch(
            f"{NEXUS}/api/v1/employees/{star_employee['id']}",
            json=_body(star_name, f"产品{suffix}", "改过的职责说明", star_employee["instructions"]),
        )
        assert renamed.status_code == 200, renamed.text
        agents = client.get(f"{AGENTSCOPE}/agent/", headers=AGENT_HEADERS).json()["agents"]
        stored = next(item for item in agents if item["id"] == star_employee["agentscope_agent_id"])
        assert "改过的职责说明" in stored["data"]["system_prompt"]

        disposable = _create_employee(
            client,
            name=f"临{suffix}",
            domain=f"临时{suffix}",
            instructions="用于删除验证。",
        )
        disposable_agent = disposable["employee"]["agentscope_agent_id"]
        assert disposable_agent in {
            item["id"]
            for item in client.get(f"{AGENTSCOPE}/agent/", headers=AGENT_HEADERS).json()["agents"]
        }
        deleted = client.delete(f"{NEXUS}/api/v1/employees/{disposable['employee']['id']}")
        assert deleted.status_code == 200, deleted.text
        remaining = {
            item["id"]
            for item in client.get(f"{AGENTSCOPE}/agent/", headers=AGENT_HEADERS).json()["agents"]
        }
        assert disposable_agent not in remaining

        first = _conversation(client, star_employee["id"])
        second_response = client.post(
            f"{NEXUS}/api/v1/employees/{star_employee['id']}/conversations",
            headers=OWNER,
            json={"name": "新对话"},
        )
        assert second_response.status_code == 200, second_response.text
        second = second_response.json()
        assert second["id"] == first["id"]
        sent_first = _send(client, first["id"], "请只回复两个汉字：收到。不要调用任何工具。")
        assert sent_first["agentscope"]["accepted"] is True
        assert "assistant" not in sent_first
        first_events = collect_events(
            f"{NEXUS}/api/v1/conversations/{first['id']}/stream",
            OWNER,
            None,
            180,
        )
        first_text = deltas(first_events)
        assert first_text, first_events
        assert any(item.get("type") == "TEXT_BLOCK_DELTA" for item in first_events)

        sent_second = _send(client, second["id"], "请只回复一个词：在。不要调用任何工具。")
        assert sent_second["agentscope"]["accepted"] is True
        second_session = sent_second["agentscope"]["session_id"]
        first_session = sent_first["agentscope"]["session_id"]
        assert first_session and second_session
        assert first_session == second_session
        sessions = client.get(
            f"{AGENTSCOPE}/sessions/",
            headers=AGENT_HEADERS,
            params={"agent_id": star_employee["agentscope_agent_id"]},
        )
        assert sessions.status_code == 200, sessions.text
        session_ids = {item["session"]["id"] for item in sessions.json().get("sessions", [])}
        assert first_session in session_ids
        assert second_session in session_ids

        stored_messages = client.get(
            f"{NEXUS}/api/v1/conversations/{first['id']}",
            headers=OWNER,
        ).json()["messages"]
        senders = [item["sender"] for item in stored_messages]
        assert [item for item in senders if item == "user"] == ["user", "user"]
        assert star_employee["id"] in senders

        peer_events: list[dict] = []
        peer_error: list[BaseException] = []

        def read_peer() -> None:
            try:
                peer_events.extend(
                    collect_events(
                        f"{AGENTSCOPE}/sessions/{peer_employee['mailbox_session_id']}/stream",
                        AGENT_HEADERS,
                        {"agent_id": peer_employee["agentscope_agent_id"]},
                        240,
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - 记录真实流错误
                peer_error.append(exc)

        reader = threading.Thread(target=read_peer, daemon=True)
        reader.start()
        contact = _send(
            client,
            first["id"],
            (
                "请立刻调用 contact_employee。"
                f"employee_name 为{peer_name}。"
                "message 为请只回复数字42。"
            ),
        )
        assert contact["agentscope"]["accepted"] is True, contact
        seen = {item["id"] for item in first_events if isinstance(item.get("id"), str)}
        caller_events = collect_events(
            f"{NEXUS}/api/v1/conversations/{first['id']}/stream",
            OWNER,
            None,
            240,
            seen,
        )
        reader.join(timeout=240)
        assert not peer_error, peer_error
        assert "contact_employee" in tool_names(caller_events), caller_events
        peer_text = deltas(peer_events)
        assert peer_text, peer_events
        assert "已把消息交给对方" not in peer_text
        assert "没有生成回复" not in peer_text
        stored_after = client.get(
            f"{NEXUS}/api/v1/conversations/{first['id']}",
            headers=OWNER,
        ).json()["messages"]
        senders_after = [item["sender"] for item in stored_after]
        assert [item for item in senders_after if item == "user"] == ["user", "user", "user"]
        assert star_employee["id"] in senders_after
        print(
            json.dumps(
                {
                    "agent_id": star_employee["agentscope_agent_id"],
                    "sessions": [first_session, second_session],
                    "private_deltas": first_text,
                    "caller_tools": tool_names(caller_events),
                    "peer_deltas": peer_text,
                },
                ensure_ascii=False,
            )
        )


def test_employee_management_against_live_agentscope():
    """创建、修改、禁用、启用、删除都打到正在运行的 Agent Service。"""
    suffix = uuid.uuid4().hex[:6]
    name = f"册{suffix}"
    domain = f"目录{suffix}"
    created_ids: list[str] = []
    with httpx.Client(timeout=_timeout(30)) as client:
        try:
            runtime = client.get(f"{NEXUS}/api/v1/runtime")
            assert runtime.status_code == 200, runtime.text
            assert runtime.json()["reachable"] is True

            knowledge = client.get(f"{NEXUS}/api/v1/knowledge")
            assert knowledge.status_code == 200, knowledge.text
            knowledge_payload = knowledge.json()
            invented_knowledge = f"不是知识库-{suffix}"
            if knowledge_payload["available"]:
                known_ids = {item["id"] for item in knowledge_payload["items"]}
                assert invented_knowledge not in known_ids
            else:
                assert knowledge_payload["items"] == []
            rejected_knowledge = client.post(
                f"{NEXUS}/api/v1/employees",
                json=_body(name, domain, "职责", "指令", knowledge=[invented_knowledge]),
            )
            assert rejected_knowledge.status_code == 422, rejected_knowledge.text

            tools = client.get(f"{NEXUS}/api/v1/tools")
            assert tools.status_code == 200, tools.text
            tool_payload = tools.json()
            assert tool_payload["available"] is True, tool_payload
            tool_ids = {item["id"] for item in tool_payload["items"]}
            assert "contact_employee" in tool_ids
            assert "Bash" in tool_ids
            assert "query_task" not in tool_ids
            rejected_tool = client.post(
                f"{NEXUS}/api/v1/employees",
                json=_body(f"具{suffix}", f"工具{suffix}", "职责", "指令", tools=["query_task"]),
            )
            assert rejected_tool.status_code == 422, rejected_tool.text

            skills = client.get(f"{NEXUS}/api/v1/skills")
            assert skills.status_code == 200, skills.text
            rejected_skill = client.post(
                f"{NEXUS}/api/v1/employees",
                json=_body(f"技{suffix}", f"技能{suffix}", "职责", "指令", skills=["not-a-real-skill"]),
            )
            assert rejected_skill.status_code == 422, rejected_skill.text

            created = _create_employee(client, name, domain, "按档案回答，不要编造同事发言。")
            employee = created["employee"]
            created_ids.append(employee["id"])
            assert created["agentscope"]["ok"] is True, created
            agent_id = employee["agentscope_agent_id"]
            assert agent_id
            listed = client.get(f"{NEXUS}/api/v1/employees", params={"q": name})
            assert listed.status_code == 200, listed.text
            assert [item["id"] for item in listed.json()["items"]] == [employee["id"]]

            agents = client.get(f"{AGENTSCOPE}/agent/", headers=AGENT_HEADERS)
            assert agents.status_code == 200, agents.text
            stored = next(item for item in agents.json()["agents"] if item["id"] == agent_id)
            assert stored["data"]["name"] == name

            renamed_domain = f"目录改{suffix}"
            edited = client.patch(
                f"{NEXUS}/api/v1/employees/{employee['id']}",
                json=_body(
                    name,
                    renamed_domain,
                    "改过的职责说明",
                    "改过的工作指令",
                    tools=["contact_employee"],
                ),
            )
            assert edited.status_code == 200, edited.text
            assert edited.json()["agentscope"]["ok"] is True, edited.text
            assert edited.json()["employee"]["tools"] == ["contact_employee"]
            agents = client.get(f"{AGENTSCOPE}/agent/", headers=AGENT_HEADERS).json()["agents"]
            stored = next(item for item in agents if item["id"] == agent_id)
            assert "改过的职责说明" in stored["data"]["system_prompt"]
            assert "改过的工作指令" in stored["data"]["system_prompt"]

            duplicate = client.post(
                f"{NEXUS}/api/v1/employees",
                json=_body(name, f"另一领域{suffix}", "职责", "指令"),
            )
            assert duplicate.status_code == 409, duplicate.text

            disabled = client.patch(
                f"{NEXUS}/api/v1/employees/{employee['id']}",
                json=_body(
                    name,
                    renamed_domain,
                    "改过的职责说明",
                    "改过的工作指令",
                    enabled=False,
                    tools=["contact_employee"],
                ),
            )
            assert disabled.status_code == 200, disabled.text
            assert disabled.json()["employee"]["enabled"] is False
            conversation = _conversation(client, employee["id"])
            refused = client.post(
                f"{NEXUS}/api/v1/conversations/{conversation['id']}/messages",
                headers=OWNER,
                json={"text": "禁用后不要开始运行", "attachment_ids": []},
            )
            assert refused.status_code == 409, refused.text

            enabled = client.patch(
                f"{NEXUS}/api/v1/employees/{employee['id']}",
                json=_body(
                    name,
                    renamed_domain,
                    "改过的职责说明",
                    "改过的工作指令",
                    enabled=True,
                    tools=["contact_employee"],
                ),
            )
            assert enabled.status_code == 200, enabled.text
            assert enabled.json()["employee"]["enabled"] is True
            accepted = client.post(
                f"{NEXUS}/api/v1/conversations/{conversation['id']}/messages",
                headers=OWNER,
                json={"text": "重新启用后请只回复一个字：在。不要调用工具。", "attachment_ids": []},
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["agentscope"]["accepted"] is True, accepted.text

            peer = _create_employee(client, f"伴{suffix}", f"同伴{suffix}", "不要主动发言。")
            peer_employee = peer["employee"]
            created_ids.append(peer_employee["id"])
            session_id = accepted.json()["agentscope"]["session_id"]
            reachable = client.get(
                f"{NEXUS}/api/v1/peers/resolve",
                params={"session_id": session_id, "name": peer_employee["name"]},
            )
            assert reachable.status_code == 200, reachable.text
            assert reachable.json()["agent_id"] == peer_employee["agentscope_agent_id"]
            client.patch(
                f"{NEXUS}/api/v1/employees/{peer_employee['id']}",
                json=_body(
                    peer_employee["name"],
                    peer_employee["domain"],
                    peer_employee["description"],
                    peer_employee["instructions"],
                    enabled=False,
                ),
            )
            blocked = client.get(
                f"{NEXUS}/api/v1/peers/resolve",
                params={"session_id": session_id, "name": peer_employee["name"]},
            )
            assert blocked.status_code == 404, blocked.text
            assert "禁用" in blocked.json()["detail"]

            deleted = client.delete(f"{NEXUS}/api/v1/employees/{employee['id']}")
            assert deleted.status_code == 200, deleted.text
            created_ids.remove(employee["id"])
            remaining = {
                item["id"]
                for item in client.get(f"{AGENTSCOPE}/agent/", headers=AGENT_HEADERS).json()["agents"]
            }
            assert agent_id not in remaining
            gone = client.get(f"{NEXUS}/api/v1/employees", params={"q": name}).json()["items"]
            assert gone == []
            print(
                json.dumps(
                    {
                        "agent_id": agent_id,
                        "knowledge_available": knowledge_payload["available"],
                        "tools": sorted(tool_ids),
                        "disabled_status": refused.status_code,
                        "enabled_accepted": True,
                        "deleted": agent_id not in remaining,
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            for employee_id in created_ids:
                client.delete(f"{NEXUS}/api/v1/employees/{employee_id}")


def test_private_chat_streams_and_isolates_anonymous_owners():
    suffix = uuid.uuid4().hex[:6]
    name = f"聊{suffix}"
    owner_a = {"X-Owner-Id": f"anon-{suffix}-a"}
    owner_b = {"X-Owner-Id": f"anon-{suffix}-b"}
    created_ids: list[str] = []
    with httpx.Client(timeout=_timeout(30)) as client:
        try:
            created = _create_employee(
                client,
                name,
                f"私聊{suffix}",
                "按用户给出的附件原文回答，不要编造文件内容。",
            )
            employee = created["employee"]
            created_ids.append(employee["id"])
            assert created["agentscope"]["ok"] is True, created
            first = client.post(
                f"{NEXUS}/api/v1/employees/{employee['id']}/conversations",
                headers=owner_a,
                json={"name": "新对话"},
            ).json()
            second = client.post(
                f"{NEXUS}/api/v1/employees/{employee['id']}/conversations",
                headers=owner_a,
                json={"name": "新对话"},
            ).json()
            assert first["id"] == second["id"]
            hidden = client.get(
                f"{NEXUS}/api/v1/employees/{employee['id']}/conversations",
                headers=owner_b,
            )
            assert hidden.json()["items"] == []

            sent = client.post(
                f"{NEXUS}/api/v1/conversations/{first['id']}/messages",
                headers=owner_a,
                json={"text": "请只回复一个字：在。不要调用工具。", "attachment_ids": []},
            )
            assert sent.status_code == 200, sent.text
            assert sent.json()["agentscope"]["accepted"] is True, sent.text
            first_session = sent.json()["agentscope"]["session_id"]
            events = collect_events(
                f"{NEXUS}/api/v1/conversations/{first['id']}/stream",
                owner_a,
                None,
                150,
            )
            assert any(item.get("type") == "TEXT_BLOCK_DELTA" for item in events), events
            again = client.post(
                f"{NEXUS}/api/v1/conversations/{first['id']}/messages",
                headers=owner_a,
                json={"text": "再说一次：在。", "attachment_ids": []},
            )
            assert again.json()["agentscope"]["session_id"] == first_session
            seen = {item["id"] for item in events if isinstance(item.get("id"), str)}
            second_events = collect_events(
                f"{NEXUS}/api/v1/conversations/{first['id']}/stream",
                owner_a,
                None,
                150,
                seen,
            )
            assert any(item.get("type") == "TEXT_BLOCK_DELTA" for item in second_events), second_events

            uploaded = client.post(
                f"{NEXUS}/api/v1/attachments",
                headers=owner_a,
                files={"file": ("run.log", b"LOG-TOKEN-88\n", "text/plain")},
            )
            assert uploaded.status_code == 201, uploaded.text
            downloaded = client.get(
                f"{NEXUS}/api/v1/attachments/{uploaded.json()['id']}",
                headers=owner_a,
            )
            assert downloaded.content == b"LOG-TOKEN-88\n"
            with_file = client.post(
                f"{NEXUS}/api/v1/conversations/{first['id']}/messages",
                headers=owner_a,
                json={"text": "附件里的标记是什么？", "attachment_ids": [uploaded.json()["id"]]},
            )
            assert with_file.json()["agentscope"]["accepted"] is True, with_file.text
            remote = None
            for _ in range(30):
                remote = client.get(
                    f"{AGENTSCOPE}/sessions/{first_session}/messages",
                    headers=AGENT_HEADERS,
                    params={"agent_id": employee["agentscope_agent_id"], "limit": 50},
                )
                assert remote.status_code == 200, remote.text
                if "LOG-TOKEN-88" in remote.text:
                    break
                time.sleep(1)
            assert remote is not None and "LOG-TOKEN-88" in remote.text
            other = client.post(
                f"{NEXUS}/api/v1/conversations/{first['id']}/messages",
                headers=owner_a,
                json={"text": "还是这一条私聊。", "attachment_ids": []},
            )
            assert other.json()["agentscope"]["accepted"] is True, other.text
            assert other.json()["agentscope"]["session_id"] == first_session
            restored = client.get(f"{NEXUS}/api/v1/conversations/{first['id']}", headers=owner_a)
            assert restored.status_code == 200, restored.text
            assert restored.json()["agentscope_session_id"] == first_session
            assert any(item["text"] == "附件里的标记是什么？" for item in restored.json()["messages"])
            blocked = client.get(f"{NEXUS}/api/v1/conversations/{first['id']}", headers=owner_b)
            assert blocked.status_code == 404

            disabled = client.patch(
                f"{NEXUS}/api/v1/employees/{employee['id']}",
                json=_body(name, f"私聊{suffix}", f"{name}的职责", employee["instructions"], enabled=False),
            )
            assert disabled.status_code == 200, disabled.text
            refused = client.post(
                f"{NEXUS}/api/v1/conversations/{first['id']}/messages",
                headers=owner_a,
                json={"text": "禁用后不要运行", "attachment_ids": []},
            )
            assert refused.status_code == 409, refused.text
            print(
                json.dumps(
                    {
                        "agent_id": employee["agentscope_agent_id"],
                        "sessions": [first_session, other.json()["agentscope"]["session_id"]],
                        "text_deltas": deltas(events)[:80],
                        "second_deltas": deltas(second_events)[:80],
                        "log_in_session": True,
                        "other_owner": 404,
                        "disabled_status": 409,
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            for employee_id in created_ids:
                client.delete(f"{NEXUS}/api/v1/employees/{employee_id}")


def _assistant_text(client: httpx.Client, session_id: str | None, agent_id: str) -> str:
    if not session_id:
        return ""
    remote = client.get(
        f"{AGENTSCOPE}/sessions/{session_id}/messages",
        headers=AGENT_HEADERS,
        params={"agent_id": agent_id, "limit": 50},
    )
    if remote.status_code != 200:
        return ""
    parts: list[str] = []
    for message in remote.json().get("messages", []):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        texts = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
        parts.append("\n".join(texts))
    return "\n".join(part for part in parts if part)


def test_team_discussion_shows_peer_identities_and_stops_repeat_contact():
    suffix = uuid.uuid4().hex[:6]
    ops_name = f"运营{suffix}"
    eng_name = f"自动{suffix}"
    tool_name = f"工具{suffix}"
    created_ids: list[str] = []
    with httpx.Client(timeout=_timeout(30)) as client:
        try:
            ops = _create_employee(
                client,
                ops_name,
                f"运行{suffix}",
                (
                    "用户要求联系同事时，必须调用 contact_employee。"
                    "employee_name 与 message 使用用户给出的原文。"
                    "不要代替对方写出答复。"
                ),
            )["employee"]
            eng = _create_employee(
                client,
                eng_name,
                f"自动工程{suffix}",
                (
                    f"收到同事提示后，调用 contact_employee，employee_name 为{tool_name}。"
                    "message 写：请看工具侧。不要代替对方写结论。"
                ),
            )["employee"]
            tools = _create_employee(
                client,
                tool_name,
                f"工具工程{suffix}",
                "收到同事提示后说明你负责工具工程。不要调用 contact_employee。",
            )["employee"]
            created_ids.extend([ops["id"], eng["id"], tools["id"]])
            channel = client.post(
                f"{NEXUS}/api/v1/channels",
                json={
                    "name": f"故障{suffix}",
                    "description": "自动化失败后的同事协助",
                    "member_ids": [ops["id"], eng["id"], tools["id"]],
                },
            )
            assert channel.status_code == 201, channel.text
            channel_id = channel.json()["id"]
            uploaded = client.post(
                f"{NEXUS}/api/v1/attachments",
                headers={"X-Owner-Id": f"channel-{suffix}"},
                files={"file": ("fail.log", b"FAIL-TOKEN-55\n", "text/plain")},
            )
            assert uploaded.status_code == 201, uploaded.text
            sent = client.post(
                f"{NEXUS}/api/v1/channels/{channel_id}/messages",
                headers={"X-Owner-Id": f"channel-{suffix}"},
                json={
                    "text": (
                        f"@{ops_name} 自动化失败了。"
                        "请立刻调用 contact_employee。"
                        f"employee_name 为{eng_name}。"
                        "message 为请看自动化失败。"
                        "不要自己写出结论。"
                    ),
                    "attachment_ids": [uploaded.json()["id"]],
                },
            )
            assert sent.status_code == 200, sent.text
            triggered = sent.json()["triggered"]
            assert [item["employee_id"] for item in triggered] == [ops["id"]]
            assert triggered[0]["accepted"] is True, sent.text
            ops_session = triggered[0]["session_id"]
            assert ops_session and ops_session != ops["mailbox_session_id"]

            collected: dict[str, list[dict]] = {ops["id"]: [], eng["id"]: [], tools["id"]: []}
            errors: list[BaseException] = []
            threads: list[threading.Thread] = []
            seen_sessions: set[str] = set()

            def read_member(employee_id: str) -> None:
                url = f"{NEXUS}/api/v1/channels/{channel_id}/members/{employee_id}/stream"
                try:
                    with httpx.Client(timeout=_timeout(45)) as stream_client:
                        with stream_client.stream("GET", url) as response:
                            response.raise_for_status()
                            for line in response.iter_lines():
                                if not line or line.startswith(":") or not line.startswith("data:"):
                                    continue
                                payload = json.loads(line[len("data:") :].strip())
                                collected[employee_id].append(payload)
                                if payload.get("type") in {"REPLY_END", "NEXUS_UPSTREAM_ERROR"}:
                                    return
                except BaseException as exc:  # noqa: BLE001 - 记下真实流错误
                    errors.append(exc)

            def watch(employee_id: str) -> None:
                if employee_id in seen_sessions:
                    return
                seen_sessions.add(employee_id)
                thread = threading.Thread(target=read_member, args=(employee_id,), daemon=True)
                threads.append(thread)
                thread.start()

            watch(ops["id"])
            deadline = time.monotonic() + 110
            ops_done_at: float | None = None
            while time.monotonic() < deadline:
                listing = client.get(f"{NEXUS}/api/v1/channels/{channel_id}/messages")
                assert listing.status_code == 200, listing.text
                for member in listing.json()["members"]:
                    if member["agentscope_session_id"]:
                        watch(member["employee_id"])
                if any(item.get("type") == "REPLY_END" for item in collected[ops["id"]]):
                    if ops_done_at is None:
                        ops_done_at = time.monotonic()
                    if time.monotonic() - ops_done_at > 25:
                        break
                time.sleep(2)
            for thread in threads:
                thread.join(timeout=40)

            log_seen = False
            for _ in range(30):
                remote = client.get(
                    f"{AGENTSCOPE}/sessions/{ops_session}/messages",
                    headers=AGENT_HEADERS,
                    params={"agent_id": ops["agentscope_agent_id"], "limit": 50},
                )
                if remote.status_code == 200 and "FAIL-TOKEN-55" in remote.text:
                    log_seen = True
                    break
                time.sleep(1)
            transcript = client.get(f"{NEXUS}/api/v1/channels/{channel_id}/messages").json()
            senders = {item["sender"] for item in transcript["messages"]}
            assert "user" in senders
            assert senders <= {"user", ops["id"], eng["id"], tools["id"]}
            remote_text = {
                employee["id"]: _assistant_text(client, bound, employee["agentscope_agent_id"])
                for employee, bound in (
                    (ops, ops_session),
                    (
                        eng,
                        next(
                            item["agentscope_session_id"]
                            for item in transcript["members"]
                            if item["employee_id"] == eng["id"]
                        ),
                    ),
                    (
                        tools,
                        next(
                            item["agentscope_session_id"]
                            for item in transcript["members"]
                            if item["employee_id"] == tools["id"]
                        ),
                    ),
                )
            }
            for item in transcript["messages"]:
                if item["sender"] == "user":
                    continue
                assert item["text"] in remote_text[item["sender"]]
                assert item["id"].startswith(f"agent_{item['sender']}_")
            first = client.get(
                f"{NEXUS}/api/v1/peers/resolve",
                params={"session_id": ops_session, "name": eng_name, "message": f"补一句{suffix}"},
            )
            second = client.get(
                f"{NEXUS}/api/v1/peers/resolve",
                params={"session_id": ops_session, "name": eng_name, "message": f"补一句{suffix}"},
            )
            assert second.status_code == 409, second.text
            if first.status_code == 200:
                assert first.json()["agent_id"] == eng["agentscope_agent_id"]
                assert first.json()["session_id"] != eng["mailbox_session_id"]
                assert "同样的话" in second.json()["detail"]
            print(
                json.dumps(
                    {
                        "ops_tools": tool_names(collected[ops["id"]]),
                        "ops_fragments": tool_fragments(collected[ops["id"]])[:400],
                        "ops_deltas": deltas(collected[ops["id"]])[:180],
                        "eng_tools": tool_names(collected[eng["id"]]),
                        "eng_fragments": tool_fragments(collected[eng["id"]])[:400],
                        "eng_deltas": deltas(collected[eng["id"]])[:180],
                        "tool_deltas": deltas(collected[tools["id"]])[:180],
                        "sessions": sorted(seen_sessions),
                        "senders": sorted(senders),
                        "log_seen": log_seen,
                        "resolve_statuses": [first.status_code, second.status_code],
                        "stream_errors": [str(item) for item in errors],
                    },
                    ensure_ascii=False,
                )
            )
            ops_tool_list = tool_names(collected[ops["id"]])
            assert not errors, errors
            assert log_seen
            assert "contact_employee" in ops_tool_list, ops_tool_list
            hidden_names = (
                "TaskCreate",
                "TaskGet",
                "TaskList",
                "AgentCreate",
                "ToolStop",
                "ScheduleCreate",
            )
            for hidden in hidden_names:
                assert hidden not in ops_tool_list, ops_tool_list
            delivered = '"status": "delivered"' in tool_fragments(collected[ops["id"]])
            assert delivered, tool_fragments(collected[ops["id"]])[:500]
            assert eng["id"] in seen_sessions
            eng_tool_list = tool_names(collected[eng["id"]])
            eng_spoke = bool(deltas(collected[eng["id"]])) or "contact_employee" in eng_tool_list
            assert eng_spoke, collected[eng["id"]]
            for hidden in hidden_names:
                assert hidden not in eng_tool_list, eng_tool_list
            if eng["id"] in seen_sessions:
                eng_member = next(
                    item for item in transcript["members"] if item["employee_id"] == eng["id"]
                )
                assert eng_member["agentscope_session_id"]
                assert eng_member["agentscope_session_id"] != eng["mailbox_session_id"]
        finally:
            for employee_id in created_ids:
                client.delete(f"{NEXUS}/api/v1/employees/{employee_id}")


def _body(
    name: str,
    domain: str,
    description: str,
    instructions: str,
    *,
    enabled: bool = True,
    knowledge: list[str] | None = None,
    skills: list[str] | None = None,
    tools: list[str] | None = None,
) -> dict:
    return {
        "name": name,
        "domain": domain,
        "description": description,
        "instructions": instructions,
        "avatar": "🌟",
        "enabled": enabled,
        "knowledge": knowledge or [],
        "skills": skills or [],
        "tools": tools or [],
    }


def _create_employee(client: httpx.Client, name: str, domain: str, instructions: str) -> dict:
    response = client.post(
        f"{NEXUS}/api/v1/employees",
        json=_body(name, domain, f"{name}的职责", instructions),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _conversation(client: httpx.Client, employee_id: str) -> dict:
    response = client.post(
        f"{NEXUS}/api/v1/employees/{employee_id}/conversations",
        headers=OWNER,
        json={"name": "新对话"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _send(client: httpx.Client, conversation_id: str, text: str) -> dict:
    response = client.post(
        f"{NEXUS}/api/v1/conversations/{conversation_id}/messages",
        headers=OWNER,
        json={"text": text, "attachment_ids": []},
    )
    assert response.status_code == 200, response.text
    return response.json()
