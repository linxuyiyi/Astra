"""平台提示词：拼装、预览与保存。"""

from __future__ import annotations

import json

from nexus_api.main import (
    DEFAULT_PLATFORM_PROMPT,
    PLATFORM_PROMPT_HEADER,
    create_app,
    employee_prompt,
    system_prompt,
)
from nexus_api.resources import ResourceStore
from tests.nexus.clients import TestClient
from tests.nexus.test_resources import FakeAgentScope, attach, employee_body

EMPLOYEE = {
    "name": "星河",
    "domain": "测试",
    "description": "职责",
    "instructions": "指令",
    "skills": [],
}


def test_assembly_order_and_empty():
    assert system_prompt(EMPLOYEE, "") == employee_prompt(EMPLOYEE)
    assert system_prompt(EMPLOYEE, "  \n") == employee_prompt(EMPLOYEE)
    final = system_prompt(EMPLOYEE, "规则A")
    assert final == f"{PLATFORM_PROMPT_HEADER}\n规则A\n\n{employee_prompt(EMPLOYEE)}"
    assert final.index("规则A") < final.index("你是星河")


def test_seed_preview_and_save_does_not_update_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    fake = FakeAgentScope()
    calls: list[tuple[str, str]] = []
    original = fake.handler

    def spy(request):
        calls.append((request.method, request.url.path))
        return original(request)

    fake.handler = spy
    with TestClient(create_app()) as client:
        attach(client, fake)
        assert client.get("/api/v1/config/platform-prompt").json() == {"text": DEFAULT_PLATFORM_PROMPT}
        created = client.post("/api/v1/employees", json=employee_body())
        assert created.status_code in (200, 201), created.text
        employee_id = created.json()["id"] if "id" in created.json() else created.json()["employee"]["id"]
        calls.clear()

        saved = client.put("/api/v1/config/platform-prompt", json={"text": "新规则"})
        assert saved.status_code == 200
        assert calls == []

        preview = client.get(f"/api/v1/config/platform-prompt/preview?employee_id={employee_id}").json()
        assert preview["platform"] == f"{PLATFORM_PROMPT_HEADER}\n新规则"
        assert preview["final"] == preview["platform"] + "\n\n" + preview["employee"]

        client.put("/api/v1/config/platform-prompt", json={"text": ""})
        empty = client.get(f"/api/v1/config/platform-prompt/preview?employee_id={employee_id}").json()
        assert empty["platform"] == "" and empty["final"] == empty["employee"]
        assert json.loads(json.dumps(empty)) == empty


def test_preview_employee_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        res1 = client.get("/api/v1/config/platform-prompt/preview?employee_id=nonexistent")
        assert res1.status_code == 404
        assert res1.json()["detail"] == "员工不存在"

        res2 = client.get("/api/v1/config/platform-prompt/preview?employee_id=")
        assert res2.status_code == 404
        assert res2.json()["detail"] == "员工不存在"

        res3 = client.get("/api/v1/config/platform-prompt/preview")
        assert res3.status_code == 404
        assert res3.json()["detail"] == "员工不存在"


def test_empty_setting_does_not_reseed(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    with TestClient(create_app()) as client:
        # First GET seeds DEFAULT_PLATFORM_PROMPT
        first = client.get("/api/v1/config/platform-prompt")
        assert first.status_code == 200
        assert first.json() == {"text": DEFAULT_PLATFORM_PROMPT}

        # Saving empty text
        put_res = client.put("/api/v1/config/platform-prompt", json={"text": ""})
        assert put_res.status_code == 200
        assert put_res.json() == {"text": ""}

        # Subsequent GET must return empty text and NOT re-seed
        second = client.get("/api/v1/config/platform-prompt")
        assert second.status_code == 200
        assert second.json() == {"text": ""}


def test_preview_byte_level_consistency_with_system_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    fake = FakeAgentScope()
    with TestClient(create_app()) as client:
        attach(client, fake)
        created = client.post("/api/v1/employees", json=employee_body(name="测试员", domain="安全"))
        assert created.status_code in (200, 201)
        employee_id = created.json()["id"] if "id" in created.json() else created.json()["employee"]["id"]
        emp_data = created.json()["employee"] if "employee" in created.json() else created.json()

        for text in ["", "  \n  ", "第一行规则\n第二行规则", "   两侧有空格的规则   "]:
            client.put("/api/v1/config/platform-prompt", json={"text": text})
            preview = client.get(f"/api/v1/config/platform-prompt/preview?employee_id={employee_id}").json()
            expected_final = system_prompt(emp_data, text)
            assert preview["final"] == expected_final
            if text.strip():
                assert preview["platform"] == f"{PLATFORM_PROMPT_HEADER}\n{text.strip()}"
                assert preview["final"] == f"{preview['platform']}\n\n{preview['employee']}"
            else:
                assert preview["platform"] == ""
                assert preview["final"] == preview["employee"]


def test_settings_failure_returns_5xx(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    fake = FakeAgentScope()
    with TestClient(create_app()) as client:
        attach(client, fake)
        created = client.post("/api/v1/employees", json=employee_body())
        employee_id = created.json()["id"] if "id" in created.json() else created.json()["employee"]["id"]

        def broken_get_setting(*_args, **_kwargs):
            raise RuntimeError("DB connection failure")

        def broken_set_setting(*_args, **_kwargs):
            raise RuntimeError("DB write failure")

        monkeypatch.setattr(ResourceStore, "get_setting", broken_get_setting)
        get_res = client.get("/api/v1/config/platform-prompt")
        assert get_res.status_code == 500
        assert get_res.json()["detail"] == "读取平台提示词失败"

        prev_res = client.get(f"/api/v1/config/platform-prompt/preview?employee_id={employee_id}")
        assert prev_res.status_code == 500
        assert prev_res.json()["detail"] == "读取平台提示词失败"

        monkeypatch.undo()
        monkeypatch.setattr(ResourceStore, "set_setting", broken_set_setting)
        put_res = client.put("/api/v1/config/platform-prompt", json={"text": "新提示词"})
        assert put_res.status_code == 500
        assert put_res.json()["detail"] == "保存平台提示词失败"
