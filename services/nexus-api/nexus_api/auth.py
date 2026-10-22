"""服务端账号与可撤销会话；浏览器自报 UID 不参与身份解析。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.responses import JSONResponse

from .db import connect, new_id, utcnow

COOKIE = "nexus_session"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(value: str, salt: str = "") -> str:
    salt = salt or secrets.token_hex(16)
    key = hashlib.scrypt(value.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"{salt}:{key}"


def user_payload(row) -> dict:
    return {
        "uid": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "permissions": json.loads(row["permissions_json"]),
    }


def session_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE, "")
    with connect() as db:
        row = (
            db.execute(
                text("""SELECT u.* FROM t_nexus_users u JOIN t_nexus_auth_sessions s
            ON s.user_id=u.id WHERE s.token_hash=:token AND s.expires_at>:now AND u.enabled=1"""),
                {"token": digest(token), "now": utcnow()},
            )
            .mappings()
            .first()
            if token
            else None
        )
    if not row:
        raise HTTPException(401, "请先登录")
    return user_payload(row)


def has_permission(user: dict, permission: str) -> bool:
    return "*" in user["permissions"] or permission in user["permissions"]


def require_user(request: Request) -> dict:
    return session_user(request)


def internal_headers() -> dict[str, str]:
    key = os.environ.get("NEXUS_INTERNAL_KEY") or os.environ.get("NEXUS_TASK_MCP_KEY", "")
    return {"X-Nexus-Internal-Key": key}


def require_internal(request: Request) -> None:
    key = internal_headers()["X-Nexus-Internal-Key"]
    if not key or not hmac.compare_digest(request.headers.get("x-nexus-internal-key", ""), key):
        raise HTTPException(401, "服务间认证失败")


class AuthenticationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(("/api/", "/agentscope-api/")):
            return await self.app(scope, receive, send)
        public = path in {"/api/v1/health", "/api/v1/runtime", "/api/health"} or path.startswith(
            "/api/v1/auth/"
        )
        if public:
            return await self.app(scope, receive, send)
        request = Request(scope)
        try:
            if (
                path == "/api/gateway/routes" and request.headers.get("x-nexus-internal-key")
            ) or path.startswith(("/api/v1/internal/", "/api/v1/peers/", "/api/v1/im/")):
                require_internal(request)
            else:
                user = session_user(request)
                scope.setdefault("state", {})["user"] = user
                if path.startswith("/api/v1/config/") and not has_permission(user, "*"):
                    raise HTTPException(403, "需要平台管理权限")
                # 配置与基础设施不向普通聊天成员开放；任务仅按群事实读取。
                if path.startswith("/agentscope-api/") or (
                    path.startswith("/api/") and not path.startswith("/api/v1/")
                ):
                    if not has_permission(user, "*"):
                        if request.method != "GET" or not path.startswith("/api/tasks"):
                            raise HTTPException(403, "需要平台管理权限")
                        channel = request.query_params.get("source_channel_id", "")
                        with connect() as db:
                            member = db.execute(
                                text("""SELECT 1 FROM t_nexus_channel_members WHERE channel_id=:c AND
                            member_type='human' AND membership_active=1 AND member_id=:u"""),
                                {"c": channel, "u": user["uid"]},
                            ).first()
                        if not member or path != "/api/tasks":
                            raise HTTPException(403, "只能读取所在群组的任务列表")
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    origin = request.headers.get("origin")
                    if origin and origin != str(request.base_url).rstrip("/"):
                        # 同源 cookie 请求；不接受跨站写入。
                        from urllib.parse import urlsplit

                        if urlsplit(origin).netloc != request.headers.get("host"):
                            raise HTTPException(403, "不允许跨站操作")
                    if path.startswith(
                        ("/api/v1/employees", "/api/v1/skills", "/api/v1/mcps", "/api/v1/knowledge")
                    ):
                        if (
                            "/conversations" not in path
                            and "/private-conversation" not in path
                            and not has_permission(user, "*")
                        ):
                            raise HTTPException(403, "需要平台管理权限")
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(
                scope, receive, send
            )
        await self.app(scope, receive, send)


class Credentials(BaseModel):
    username: str = Field(pattern=r"^[a-zA-Z0-9_.-]{3,40}$")
    password: str = Field(min_length=10, max_length=128)
    display_name: str = Field(default="", max_length=40)


def install_auth(app):
    router = APIRouter(prefix="/api/v1/auth")

    def login_response(db, row, response):
        token = secrets.token_urlsafe(32)
        db.execute(
            text("INSERT INTO t_nexus_auth_sessions VALUES (:t,:u,:e)"),
            {"t": digest(token), "u": row["id"], "e": (datetime.now(UTC) + timedelta(days=7)).isoformat()},
        )
        db.commit()
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            max_age=604800,
            secure=os.environ.get("NEXUS_COOKIE_SECURE", "0") == "1",
        )
        return user_payload(row)

    @router.post("/register", status_code=201)
    def register(body: Credentials, response: Response):
        with connect() as db:
            # 首账号权限使用唯一固定引导标记，避免并发注册产生多个管理员。
            first = db.execute(text("SELECT 1 FROM t_nexus_users LIMIT 1")).first() is None
            uid = "usr_bootstrap" if first else new_id("usr")
            try:
                db.execute(
                    text("""INSERT INTO t_nexus_users
                    (id,username,display_name,password_hash,permissions_json,enabled,created_at)
                    VALUES (:i,:n,:d,:p,:r,1,:t)"""),
                    {
                        "i": uid,
                        "n": body.username.lower(),
                        "d": body.display_name.strip() or body.username,
                        "p": password_hash(body.password),
                        "r": json.dumps(["*"] if first else ["chat"]),
                        "t": utcnow(),
                    },
                )
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise HTTPException(409, "用户名已存在或首账号正在创建，请重试") from exc
            row = db.execute(text("SELECT * FROM t_nexus_users WHERE id=:i"), {"i": uid}).mappings().one()
            return login_response(db, row, response)

    @router.post("/login")
    def login(body: Credentials, response: Response):
        with connect() as db:
            row = (
                db.execute(
                    text("SELECT * FROM t_nexus_users WHERE username=:n AND enabled=1"),
                    {"n": body.username.lower()},
                )
                .mappings()
                .first()
            )
            stored = row["password_hash"] if row else password_hash("invalid-password")
            valid = hmac.compare_digest(password_hash(body.password, stored.split(":")[0]), stored)
            if not row or not valid:
                raise HTTPException(401, "用户名或密码错误")
            return login_response(db, row, response)

    @router.post("/logout")
    def logout(request: Request, response: Response):
        with connect() as db:
            db.execute(
                text("DELETE FROM t_nexus_auth_sessions WHERE token_hash=:t"),
                {"t": digest(request.cookies.get(COOKIE, ""))},
            )
            db.commit()
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @router.get("/session")
    def me(request: Request):
        return session_user(request)

    @app.get("/api/v1/users")
    def users(request: Request):
        if not has_permission(session_user(request), "*"):
            raise HTTPException(403, "需要平台管理权限")
        with connect() as db:
            return {
                "items": [
                    user_payload(r)
                    for r in db.execute(
                        text("SELECT * FROM t_nexus_users ORDER BY created_at LIMIT 200")
                    ).mappings()
                ]
            }

    @app.patch("/api/v1/users/{user_id}/permissions")
    async def permissions(user_id: str, request: Request):
        if not has_permission(session_user(request), "*"):
            raise HTTPException(403, "需要平台管理权限")
        body = await request.json()
        values = body.get("permissions")
        if (
            not isinstance(values, list)
            or len(values) > 50
            or any(not isinstance(v, str) or len(v) > 100 for v in values)
        ):
            raise HTTPException(422, "权限列表无效")
        if user_id == "usr_bootstrap":
            raise HTTPException(409, "不能移除初始管理员权限")
        with connect() as db:
            result = db.execute(
                text("UPDATE t_nexus_users SET permissions_json=:p WHERE id=:u"),
                {"p": json.dumps(values), "u": user_id},
            )
            if result.rowcount != 1:
                raise HTTPException(404, "用户不存在")
            db.commit()
        return {"ok": True}

    app.include_router(router)
    app.add_middleware(AuthenticationMiddleware)
