from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "mcp_server" / "src"
DEFAULT_BASE_URL = "http://127.0.0.1:8000/api/v1"


def login() -> tuple[str, str]:
    base_url = (os.getenv("NR_API_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    username = (os.getenv("NR_DEMO_USERNAME") or "").strip()
    password = os.getenv("NR_DEMO_PASSWORD") or ""
    if not username or not password:
        raise SystemExit("演示验证需要设置 NR_DEMO_USERNAME 与 NR_DEMO_PASSWORD")

    with httpx.Client(base_url=f"{base_url}/", timeout=10, trust_env=False) as client:
        csrf_response = client.get("auth/csrf/")
        csrf_response.raise_for_status()
        csrf_token = client.cookies.get("csrftoken")
        if not csrf_token:
            raise SystemExit("未能从本地演示服务获取 CSRF Cookie")
        login_response = client.post(
            "auth/login/",
            json={"username": username, "password": password},
            headers={"X-CSRFToken": csrf_token},
        )
        if login_response.status_code != 200:
            raise SystemExit(f"登录本地演示服务失败：HTTP {login_response.status_code}")
        session_id = client.cookies.get("sessionid")
        if not session_id:
            raise SystemExit("登录成功但未获得 sessionid Cookie")
    return base_url, session_id


async def call_tools(base_url: str, session_id: str) -> dict[str, Any]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(SRC_DIR),
        "NR_API_BASE_URL": base_url,
        "NR_SESSION_COOKIE": f"sessionid={session_id}",
        "NR_DEFAULT_STATUSES": os.getenv("NR_DEFAULT_STATUSES", "RELEASED"),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "nr_version_mcp"],
        env=env,
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            search = await session.call_tool(
                "search_product_versions",
                {
                    "keyword": os.getenv("NR_DEMO_KEYWORD", "v1"),
                    "product_name": os.getenv("NR_DEMO_PRODUCT", "NR 测试套件"),
                    "page": 1,
                    "page_size": 1,
                },
            )
            if search.isError:
                raise SystemExit(f"search_product_versions 调用失败：{search.content[0].text}")
            search_payload = search.structuredContent or {}
            results = search_payload.get("results") or []
            if not results:
                raise SystemExit("搜索没有返回结果，无法继续验证版本详情")

            filtered = await session.call_tool(
                "search_product_versions",
                {
                    "product_name": os.getenv("NR_DEMO_PRODUCT", "NR 测试套件"),
                    "release_date_from": "2026-01-01",
                    "release_date_to": "2026-12-31",
                    "page": 2,
                    "page_size": 1,
                },
            )
            if filtered.isError:
                raise SystemExit(f"日期筛选调用失败：{filtered.content[0].text}")

            detail = await session.call_tool(
                "get_product_version", {"version_id": results[0]["id"]}
            )
            if detail.isError:
                raise SystemExit(f"get_product_version 调用失败：{detail.content[0].text}")
            return {
                "tools": [tool.name for tool in tools.tools],
                "search": search_payload,
                "filtered": filtered.structuredContent or {},
                "detail": detail.structuredContent or {},
            }


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    base_url, session_id = login()
    payload = asyncio.run(call_tools(base_url, session_id))
    search = payload["search"]
    statuses = {item["status"] for item in search["results"]}
    print("MCP tools:", ", ".join(payload["tools"]))
    print(
        "search:",
        json.dumps(
            {
                "count": search["count"],
                "page": search["page"],
                "page_size": search["page_size"],
                "applied_statuses": search["applied_statuses"],
                "client_side_filtering": search["client_side_filtering"],
                "versions": [
                    {
                        "id": item["id"],
                        "product_name": item["product_name"],
                        "version": item["version"],
                        "status": item["status"],
                    }
                    for item in search["results"]
                ],
            },
            ensure_ascii=False,
        ),
    )
    filtered = payload["filtered"]
    print(
        "date filter:",
        json.dumps(
            {
                "count": filtered["count"],
                "page": filtered["page"],
                "page_size": filtered["page_size"],
                "applied_statuses": filtered["applied_statuses"],
                "client_side_filtering": filtered["client_side_filtering"],
                "applied_release_date": filtered["applied_release_date"],
                "versions": [item["version"] for item in filtered["results"]],
            },
            ensure_ascii=False,
        ),
    )
    print(
        "detail:",
        json.dumps(
            {
                "id": payload["detail"]["id"],
                "product_name": payload["detail"]["product_name"],
                "version": payload["detail"]["version"],
                "status": payload["detail"]["status"],
                "description": payload["detail"]["description"],
            },
            ensure_ascii=False,
        ),
    )
    if statuses - set(search["applied_statuses"]):
        raise SystemExit(f"返回了非预期状态：{sorted(statuses - set(search['applied_statuses']))}")
    print("demo verification passed")


if __name__ == "__main__":
    main()
