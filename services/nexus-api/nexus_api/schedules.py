"""官方日程的员工/身份适配；不持久化日程、不运行计时器。"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text

from .agentscope import AgentScopeError
from .db import connect, employee_from_row

SCHEDULE_USER_PREFIX = "nexus-schedule-"


def schedule_user_id(uid: str) -> str:
    return SCHEDULE_USER_PREFIX + hashlib.sha256(uid.encode()).hexdigest()


class ScheduleFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=16000)
    cron_expression: str = Field(min_length=9, max_length=120)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)
    stateful: bool = False
    enabled: bool = True

    @field_validator("name", "description", "cron_expression", "timezone")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value


class ScheduleCreate(ScheduleFields):
    employee_id: str = Field(min_length=1, max_length=64)


class ScheduleToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def schedule_router(require_owner, model_for) -> APIRouter:
    router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])

    def employees() -> dict[str, dict]:
        with connect() as db:
            return {
                row["agentscope_agent_id"]: employee_from_row(row)
                for row in db.execute(text("SELECT * FROM t_nexus_employees")).mappings()
                if row["agentscope_agent_id"]
            }

    async def upstream(request, uid, method, path, **kwargs):
        try:
            return await request.app.state.agentscope.request(
                method, path, headers={"X-User-ID": schedule_user_id(uid)}, **kwargs
            )
        except AgentScopeError as exc:
            status = exc.status_code if exc.status_code in {404, 409, 422} else 502
            detail = str(exc) if exc.status_code else "AgentScope 日程服务不可达，请稍后重试"
            raise HTTPException(status, detail) from exc

    async def records(request, uid):
        response = await upstream(request, uid, "GET", "/schedule/")
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("schedules"), list):
            raise HTTPException(502, "AgentScope 日程响应无效")
        return payload["schedules"]

    async def owned(request, uid, schedule_id):
        for item in await records(request, uid):
            if item["id"] == schedule_id:
                return item
        raise HTTPException(404, "日程不存在")

    def available(agent_id):
        employee = employees().get(agent_id)
        if employee is None or not employee["enabled"]:
            raise HTTPException(409, "数字员工已禁用或未绑定 Agent，请先检查员工配置")
        return employee

    def public(item, directory, uid):
        employee = directory.get(item["agent_id"], {})
        data = dict(item["data"])
        data.pop("chat_model_config", None)
        return {
            "id": item["id"],
            "employee_id": employee.get("id"),
            "employee_name": employee.get("name", "已删除员工"),
            "owner_uid": uid,
            "created_at": item.get("created_at"),
            "data": data,
        }

    @router.get("")
    async def listing(
        request: Request,
        owner: str = Depends(require_owner),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        employee_id: str | None = Query(None, max_length=64),
        enabled: bool | None = None,
        q: str = Query("", max_length=120),
    ):
        rows = await records(request, owner)
        rows.sort(key=lambda row: row.get("created_at", ""), reverse=True)
        directory = employees()
        if employee_id:
            rows = [row for row in rows if directory.get(row["agent_id"], {}).get("id") == employee_id]
        if enabled is not None:
            rows = [row for row in rows if row["data"]["enabled"] == enabled]
        if q.strip():
            keyword = q.strip().casefold()
            rows = [
                row
                for row in rows
                if keyword in (row["data"]["name"] + " " + row["data"]["description"]).casefold()
            ]
        start = (page - 1) * page_size
        return {
            "employee_options": [{"id": item["id"], "name": item["name"]} for item in directory.values()],
            "items": [public(row, directory, owner) for row in rows[start : start + page_size]],
            "total": len(rows),
        }

    @router.post("", status_code=201)
    async def create(body: ScheduleCreate, request: Request, owner: str = Depends(require_owner)):
        employee = next((e for e in employees().values() if e["id"] == body.employee_id), None)
        if employee is None:
            raise HTTPException(409, "数字员工不存在或未绑定 Agent")
        available(employee["agentscope_agent_id"])
        client = request.app.state.agentscope
        try:
            config = await model_for(client, employee)
        except AgentScopeError as exc:
            raise HTTPException(exc.status_code or 502, str(exc)) from exc
        if not config:
            raise HTTPException(502, "AgentScope 未连接")
        payload = body.model_dump(exclude={"employee_id"})
        payload.update(
            agent_id=employee["agentscope_agent_id"], chat_model_config=config, permission_mode="dont_ask"
        )
        response = await upstream(request, owner, "POST", "/schedule/", json=payload)
        return response.json()

    @router.put("/{schedule_id}")
    async def update(
        schedule_id: str, body: ScheduleFields, request: Request, owner: str = Depends(require_owner)
    ):
        item = await owned(request, owner, schedule_id)
        if body.enabled:
            available(item["agent_id"])
        response = await upstream(
            request, owner, "PATCH", f"/schedule/{quote(schedule_id, safe='')}", json=body.model_dump()
        )
        return public(response.json(), employees(), owner)

    @router.patch("/{schedule_id}")
    async def toggle(
        schedule_id: str, body: ScheduleToggle, request: Request, owner: str = Depends(require_owner)
    ):
        item = await owned(request, owner, schedule_id)
        if body.enabled:
            available(item["agent_id"])
        response = await upstream(
            request, owner, "PATCH", f"/schedule/{quote(schedule_id, safe='')}", json=body.model_dump()
        )
        return public(response.json(), employees(), owner)

    @router.delete("/{schedule_id}", status_code=204)
    async def delete(schedule_id: str, request: Request, owner: str = Depends(require_owner)):
        await owned(request, owner, schedule_id)
        await upstream(request, owner, "DELETE", f"/schedule/{quote(schedule_id, safe='')}")
        return Response(status_code=204)

    async def sessions_for(request, owner, schedule_id):
        item = await owned(request, owner, schedule_id)
        response = await upstream(request, owner, "GET", f"/schedule/{quote(schedule_id, safe='')}/sessions")
        return item, response.json()["sessions"]

    @router.get("/{schedule_id}/sessions")
    async def sessions(
        schedule_id: str,
        request: Request,
        owner: str = Depends(require_owner),
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ):
        _, rows = await sessions_for(request, owner, schedule_id)
        # 不把官方持久化的 Agent state/模型配置送往浏览器。
        start = (page - 1) * page_size
        return {
            "items": [
                {"id": row["id"], "created_at": row.get("created_at")}
                for row in rows[start : start + page_size]
            ],
            "total": len(rows),
        }

    @router.get("/{schedule_id}/sessions/{session_id}/messages")
    async def messages(
        schedule_id: str,
        session_id: str,
        request: Request,
        owner: str = Depends(require_owner),
        before: str | None = Query(None, max_length=160),
    ):
        item, rows = await sessions_for(request, owner, schedule_id)
        if not any(row["id"] == session_id for row in rows):
            raise HTTPException(404, "执行会话不存在")
        params: dict[str, Any] = {"agent_id": item["agent_id"], "limit": 100}
        if before:
            params["before"] = before
        response = await upstream(
            request, owner, "GET", f"/sessions/{quote(session_id, safe='')}/messages", params=params
        )
        return response.json()

    return router
