from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class OperationBase(BaseModel):
    operation_type: str = Field(description="操作类型")
    risk_level: str = Field(description="风险级别 (L1, L2)")
    request_json: dict[str, Any] = Field(default_factory=dict, description="原始请求参数")
    preview_json: dict[str, Any] = Field(default_factory=dict, description="需要向用户展示的预览数据")

class OperationResponse(OperationBase):
    id: str
    operation_id: str
    conversation_id: str
    message_id: str | None = None
    user_id: str
    status: str = Field(description="状态")
    result_json: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime | None = None
    confirmed_at: datetime | None = None
    executed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
