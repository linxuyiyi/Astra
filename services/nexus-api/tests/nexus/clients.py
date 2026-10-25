"""业务回归客户端：按产品约定记录临时 UID，不创建本地账号或会话。"""

import os

import httpx
from fastapi.testclient import TestClient as AnonymousClient


def session_headers(uid="u1"):
    return {"Cookie": f"nexus_uid={uid}"}


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
