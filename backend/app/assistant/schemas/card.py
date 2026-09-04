from typing import Any

from pydantic import BaseModel, Field


class CardAction(BaseModel):
    type: str = Field(description="动作类型 (navigate, assistant_operation 等)")
    label: str = Field(description="按钮文案")
    target: str | None = Field(default=None, description="导航目标 URL 或路由")
    operation: str | None = Field(default=None, description="绑定的后台 operation 类型")

class BusinessCard(BaseModel):
    type: str = Field(description="业务对象类型 (task, employee, run, knowledge 等)")
    id: str | None = Field(default=None, description="业务对象 ID")
    title: str = Field(description="卡片标题")
    status: str | None = Field(default=None, description="状态 (如 RUNNING, IDLE)")
    fields: list[dict[str, Any]] = Field(default_factory=list, description="要展示的字段键值对")
    actions: list[CardAction] = Field(default_factory=list, description="支持的操作")
