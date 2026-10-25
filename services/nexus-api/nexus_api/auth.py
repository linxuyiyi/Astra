"""内部服务认证与临时 UID 标识；不提供本地账号、登录或用户权限。"""

from __future__ import annotations

import hashlib
import hmac
import os
from urllib.parse import unquote

from fastapi import HTTPException, Request
from sqlalchemy import text
from starlette.responses import JSONResponse

from .db import connect
from .sql import insert_record


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def require_user(request: Request) -> dict:
    """临时记录 UID，不代表认证；公司登录接入后替换这里。"""
    uid = (request.headers.get("x-owner-id") or unquote(request.cookies.get("nexus_uid", ""))).strip()
    if not uid:
        raise HTTPException(401, "需要输入临时 UID")
    if len(uid) > 64 or any(ord(char) < 32 for char in uid):
        raise HTTPException(422, "UID 长度不能超过 64，且不能包含控制字符")
    with connect() as db:
        if not db.execute(text("SELECT 1 FROM t_nexus_identity_profiles WHERE id=:u"), {"u": uid}).first():
            insert_record(
                db, "t_nexus_identity_profiles", {"id": uid, "display_name": uid}, ignore_keys=["id"]
            )
            db.commit()
    return {"uid": uid, "display_name": uid}


def internal_headers() -> dict[str, str]:
    key = os.environ.get("NEXUS_INTERNAL_KEY") or os.environ.get("NEXUS_TASK_MCP_KEY", "")
    return {"X-Nexus-Internal-Key": key}


def require_internal(request: Request) -> None:
    key = internal_headers()["X-Nexus-Internal-Key"]
    if not key or not hmac.compare_digest(request.headers.get("x-nexus-internal-key", ""), key):
        raise HTTPException(401, "服务间认证失败")


class IdentityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(("/api/", "/agentscope-api/")):
            return await self.app(scope, receive, send)
        request = Request(scope)
        try:
            if (
                path == "/api/gateway/routes" and request.headers.get("x-nexus-internal-key")
            ) or path.startswith(("/api/v1/internal/", "/api/v1/peers/", "/api/v1/im/")):
                require_internal(request)
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(
                scope, receive, send
            )
        await self.app(scope, receive, send)


def install_identity(app):
    app.add_middleware(IdentityMiddleware)
