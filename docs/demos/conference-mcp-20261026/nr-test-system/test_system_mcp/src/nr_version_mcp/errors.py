from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApiError(Exception):
    """转发既有 HTTP API 失败时使用的稳定错误。"""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    status: int | None = None

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }
        if self.status is not None:
            payload["upstream_status"] = self.status
        return payload


@dataclass
class ToolInputError(Exception):
    """工具参数在本地校验失败。"""

    message: str
    details: dict[str, Any] = field(default_factory=dict)
    code: str = "validation_error"

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}
