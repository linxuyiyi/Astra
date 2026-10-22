"""业务回归客户端：种入真实服务端会话，测试身份选择不再成为产品认证入口。

新认证/越权用例使用原生 TestClient。历史业务用例的 UID 仅在这里转换为测试会话，
实际请求走生产 AuthenticationMiddleware；没有关闭认证或覆盖依赖。
"""

import os
import secrets
from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient as AnonymousClient
from sqlalchemy import text

from nexus_api.auth import digest
from nexus_api.db import connect, utcnow
from nexus_api.sql import insert_record


def session_headers(uid="u1"):
    token = secrets.token_urlsafe(32)
    with connect() as db:
        insert_record(
            db,
            "t_nexus_users",
            {
                "id": uid,
                "username": uid,
                "display_name": uid,
                "password_hash": "fixture-only",
                "permissions_json": '["*"]',
                "enabled": 1,
                "created_at": utcnow(),
            },
            ignore_keys=["id"],
        )
        db.execute(
            text("INSERT INTO t_nexus_auth_sessions VALUES (:t,:u,:e)"),
            {"t": digest(token), "u": uid, "e": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        )
        db.commit()
    return {"Cookie": f"nexus_session={token}"}


class TestClient(AnonymousClient):
    __test__ = False

    def build_request(self, method, url, **kwargs):
        headers = {**self.headers, **(kwargs.pop("headers", None) or {})}
        supplied = {k.lower(): v for k, v in headers.items()}
        if "cookie" not in supplied:
            headers.update(
                session_headers(supplied.get("x-verified-uid") or supplied.get("x-owner-id") or "u1")
            )
        if httpx.URL(str(url)).path.startswith(("/api/v1/internal/", "/api/v1/peers/", "/api/v1/im/")):
            headers.setdefault("X-Nexus-Internal-Key", os.environ["NEXUS_INTERNAL_KEY"])
        return super().build_request(method, url, headers=headers, **kwargs)
