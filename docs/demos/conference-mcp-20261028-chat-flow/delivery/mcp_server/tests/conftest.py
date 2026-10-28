from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

DEMO_VERSIONS: list[dict[str, Any]] = [
    {
        "id": "11111111-1111-4111-8111-111111111111",
        "product_name": "NR 测试套件",
        "version": "v1.2.0",
        "status": "RELEASED",
        "release_date": "2026-06-01",
        "description": "稳定版本",
        "is_default": True,
        "created_by_name": "demo",
        "updated_by_name": "demo",
        "created_at": "2026-05-01T08:00:00Z",
        "updated_at": "2026-06-02T08:00:00Z",
    },
    {
        "id": "22222222-2222-4222-8222-222222222222",
        "product_name": "NR 测试套件",
        "version": "v1.1.0",
        "status": "RELEASED",
        "release_date": "2026-03-01",
        "description": "上一稳定版本",
        "is_default": False,
        "created_by_name": "demo",
        "updated_by_name": "demo",
        "created_at": "2026-02-01T08:00:00Z",
        "updated_at": "2026-03-02T08:00:00Z",
    },
    {
        "id": "33333333-3333-4333-8333-333333333333",
        "product_name": "NR 测试套件",
        "version": "v2.0.0-beta",
        "status": "DRAFT",
        "release_date": None,
        "description": "开发中版本",
        "is_default": False,
        "created_by_name": "demo",
        "updated_by_name": "demo",
        "created_at": "2026-08-01T08:00:00Z",
        "updated_at": "2026-09-01T08:00:00Z",
    },
    {
        "id": "44444444-4444-4444-8444-444444444444",
        "product_name": "NR 测试套件",
        "version": "v0.9.0",
        "status": "ARCHIVED",
        "release_date": "2025-12-01",
        "description": "历史版本",
        "is_default": False,
        "created_by_name": "demo",
        "updated_by_name": "demo",
        "created_at": "2025-11-01T08:00:00Z",
        "updated_at": "2025-12-02T08:00:00Z",
    },
    {
        "id": "55555555-5555-4555-8555-555555555555",
        "product_name": "其他产品",
        "version": "v3.1.0",
        "status": "RELEASED",
        "release_date": "2026-08-01",
        "description": "其他产品的稳定版本",
        "is_default": True,
        "created_by_name": "demo",
        "updated_by_name": "demo",
        "created_at": "2026-07-01T08:00:00Z",
        "updated_at": "2026-08-02T08:00:00Z",
    },
]


@dataclass
class FakeApi:
    base_url: str
    expected_cookie: str
    versions: list[dict[str, Any]]
    requests: list[dict[str, Any]]


class _Handler(BaseHTTPRequestHandler):
    api: FakeApi

    def do_GET(self) -> None:  # noqa: N802
        self.api.requests.append({"path": self.path, "cookie": self.headers.get("Cookie")})
        if self.headers.get("Cookie") != self.api.expected_cookie:
            self._send(401, {"code": "unauthenticated", "message": "请先登录"})
            return

        parsed = urlparse(self.path)
        prefix = "/api/v1/system/versions/"
        if parsed.path == prefix:
            query = parse_qs(parsed.query)
            self._list(query)
            return
        if parsed.path.startswith(prefix) and parsed.path.endswith("/"):
            version_id = parsed.path[len(prefix) : -1]
            for item in self.api.versions:
                if item["id"] == version_id:
                    self._send(200, item)
                    return
            self._send(404, {"code": "not_found", "message": "请求的资源不存在"})
            return
        self._send(404, {"code": "not_found", "message": "请求的资源不存在"})

    def _list(self, query: dict[str, list[str]]) -> None:
        items = list(self.api.versions)
        search = query.get("search", [""])[0]
        product_name = query.get("product_name", [""])[0]
        status = query.get("status", [""])[0]
        if search:
            items = [
                item
                for item in items
                if search.lower() in item["product_name"].lower()
                or search.lower() in item["version"].lower()
            ]
        if product_name:
            items = [item for item in items if item["product_name"] == product_name]
        if status:
            items = [item for item in items if item["status"] == status]
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        page = int(query.get("page", ["1"])[0])
        page_size = int(query.get("page_size", ["20"])[0])
        offset = (page - 1) * page_size
        self._send(
            200,
            {
                "count": len(items),
                "page": page,
                "page_size": page_size,
                "results": items[offset : offset + page_size],
            },
        )

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def fake_api() -> FakeApi:
    api = FakeApi(
        base_url="http://127.0.0.1:8000/api/v1",
        expected_cookie="sessionid=test-session",
        versions=[dict(item) for item in DEMO_VERSIONS],
        requests=[],
    )

    class Handler(_Handler):
        pass

    Handler.api = api
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    api.base_url = f"http://127.0.0.1:{port}/api/v1"
    try:
        yield api
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
